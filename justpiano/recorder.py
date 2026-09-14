"""
Performance capture: records raw MIDI events with timestamps and exports them
as a Standard MIDI File, or renders them back through the piano engine to WAV.

Two buffers are kept:
  * `take`    - the explicit recording (Start/Stop Recording)
  * `session` - everything played since launch, so you can always save that
                thing you just improvised but forgot to hit record for

Both are `EventRing`s rather than lists of tuples: a MIDI event is a timestamp
and three bytes, which CPython stores as a 120-byte tuple of boxed objects and
numpy stores as 11. At the session limit that is 4.2 MiB instead of 46.2, and
the cap is enforced one event at a time instead of by periodically deleting the
front quarter of a 400,000-element list -- an 8.7 ms stall on the rtmidi
callback thread, which is a MIDI timing hiccup you can hear.
"""

from __future__ import annotations

import os
import threading
import time
import wave
from typing import Iterable, NamedTuple, Optional

import numpy as np

SESSION_LIMIT = 400_000
TICKS_PER_BEAT = 480
EXPORT_BPM = 120.0

#: Initial `EventRing` capacity, in events. 45 KB: small enough that a session
#: nobody played into costs nothing, large enough that the growth doublings are
#: over long before anyone finishes their first piece.
RING_START = 4096

#: Events boxed per step in `rows()`. The tuples are built without any lock
#: held, but a single `tolist()` of 400,000 elements is one uninterruptible C
#: call that keeps the GIL for milliseconds, which the rtmidi callback thread
#: waits out; in chunks this size nothing holds it for long enough to hear.
ROWS_CHUNK = 8192

NOTE_OFF = 0x80
NOTE_ON = 0x90
CONTROL_CHANGE = 0xB0
PITCH_BEND = 0xE0


def _is_note_on(status: int, d2: int) -> bool:
    """True for a real note-on: `NOTE_ON` with a running-status zero velocity is
    a note-*off*, and counting it would inflate the menu's note tally."""
    return (status & 0xF0) == NOTE_ON and d2 > 0


def rows(columns) -> list[tuple[float, int, int, int]]:
    """Box four event columns into the list of tuples the exporters want.

    Deliberately not a method on `EventRing`: this is the expensive half of a
    snapshot (400,000 events become 400,000 boxed tuples, 46 MiB and tens of
    milliseconds) and it must never run while a lock the rtmidi callback thread
    needs is held. See `EventRing.columns`. Boxed `ROWS_CHUNK` events at a time
    so the callback thread never queues behind one long C call either.
    """
    when, status, d1, d2 = columns
    out: list[tuple[float, int, int, int]] = []
    for lo in range(0, when.size, ROWS_CHUNK):
        hi = lo + ROWS_CHUNK
        out.extend(zip(when[lo:hi].tolist(), status[lo:hi].tolist(),
                       d1[lo:hi].tolist(), d2[lo:hi].tolist()))
    return out


class EventRing:
    """A bounded FIFO of `(when, status, d1, d2)` MIDI events.

    Append and evict are both O(1) and neither rescans: the note-on tally is
    adjusted by one as events go in and (at the cap) by one as they fall off the
    front, so `Recorder.stats()` never has to walk the buffer and the rtmidi
    thread never pays for a bulk trim.

    Storage grows geometrically to `limit` and only then wraps, so an idle
    session holds 45 KB rather than the 4.2 MiB it is allowed to reach. The
    columns are separate arrays instead of one structured dtype so that the part
    of a snapshot that has to happen under the recorder's lock is four
    fancy-index array copies (`columns`), with the boxing into tuples (`rows`)
    left to the caller, after the lock is released.

    `limit=0` means unbounded, which is what a take wants: a recording the user
    asked for must never quietly lose its beginning.
    """

    __slots__ = ("_when", "_status", "_d1", "_d2", "_start", "_count", "_notes",
                 "limit")

    def __init__(self, limit: int = 0) -> None:
        self.limit = int(limit)
        self._alloc(min(RING_START, self.limit) if self.limit else RING_START)
        self._start = 0
        self._count = 0
        self._notes = 0

    def _alloc(self, capacity: int) -> None:
        self._when = np.empty(capacity, dtype=np.float64)
        self._status = np.empty(capacity, dtype=np.uint8)
        self._d1 = np.empty(capacity, dtype=np.uint8)
        self._d2 = np.empty(capacity, dtype=np.uint8)

    @property
    def capacity(self) -> int:
        return self._when.size

    @property
    def notes(self) -> int:
        """Note-on events held in the buffer, maintained incrementally.

        A count of events, not of keys down: a note-on that has already been
        released is still one of these until it falls off the front.
        """
        return self._notes

    def __len__(self) -> int:
        return self._count

    def clear(self) -> None:
        """Drop everything and give the memory back."""
        self._alloc(min(RING_START, self.limit) if self.limit else RING_START)
        self._start = 0
        self._count = 0
        self._notes = 0

    def _grow(self) -> None:
        """Double the capacity (up to `limit`).

        Always called while the buffer is still linear, so this is one slice per
        column: `_start` leaves zero for the first time in `append`'s eviction
        branch, which is reached only once `capacity == limit` -- and a ring at
        its limit never grows again.
        """
        new_cap = self.capacity * 2
        if self.limit:
            new_cap = min(new_cap, self.limit)
        old = (self._when, self._status, self._d1, self._d2)
        count = self._count
        self._alloc(new_cap)
        for src, dst in zip(old, (self._when, self._status, self._d1, self._d2)):
            dst[:count] = src[:count]

    def append(self, when: float, status: int, d1: int, d2: int) -> None:
        # Mask once, up front: the tally and the stored bytes have to be drawn
        # from the same values, or `notes` disagrees with a rescan of the buffer
        # (d2=256 was stored as a note-off and counted as a note-on).
        status &= 0xFF
        d1 &= 0xFF
        d2 &= 0xFF
        if self._count == self.capacity:
            if not self.limit or self.capacity < self.limit:
                self._grow()
            else:
                # Full and capped: the oldest event falls off the front. One
                # read tells us whether the tally has to come down with it --
                # no scan, no bulk delete, no 8.7 ms hole in the MIDI stream.
                i = self._start
                if _is_note_on(int(self._status[i]), int(self._d2[i])):
                    self._notes -= 1
                self._start = (i + 1) % self.capacity
                self._count -= 1
        i = (self._start + self._count) % self.capacity
        self._when[i] = when
        self._status[i] = status
        self._d1[i] = d1
        self._d2[i] = d2
        self._count += 1
        if _is_note_on(status, d2):
            self._notes += 1

    def _order(self) -> np.ndarray:
        """Indices of the held events, oldest first."""
        end = self._start + self._count
        if end <= self.capacity:
            return np.arange(self._start, end)
        return np.concatenate((np.arange(self._start, self.capacity),
                               np.arange(0, end - self.capacity)))

    def columns(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Copies of the held events as four arrays, oldest first.

        This is the whole of the work a snapshot has to do while the recorder's
        lock is held: four fancy-index copies of a full session buffer cost
        1.2-3.7 ms, against the 52-59 ms it takes to box the same 400,000 events
        into tuples -- which is why the boxing is `rows()`' job instead.
        """
        idx = self._order()
        return (self._when[idx], self._status[idx], self._d1[idx], self._d2[idx])

    def snapshot(self) -> list[tuple[float, int, int, int]]:
        """A plain list of tuples, oldest first, for the exporters.

        Only safe on a buffer nobody else is appending to. Anything sharing this
        one with the rtmidi callback thread must take `columns()` under the lock
        and call `rows()` after releasing it, the way `Recorder.snapshot()` does.
        """
        return rows(self.columns())

    def span(self) -> float:
        """Seconds between the first and last event held (0.0 when empty)."""
        if self._count < 2:
            return 0.0
        last = (self._start + self._count - 1) % self.capacity
        return float(self._when[last]) - float(self._when[self._start])


class RecorderStats(NamedTuple):
    """What the UI needs to label the record menu, in one locked read."""

    take_notes: int       # note-on events in the current take
    take_seconds: float   # time span of the current take
    session_notes: int    # note-on events held in the session buffer


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.take = EventRing()                      # unbounded: it is a take
        self.session = EventRing(SESSION_LIMIT)
        self.recording = False
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None

    # ------------------------------------------------------------------ input
    def handle(self, status: int, d1: int, d2: int,
               when: Optional[float] = None) -> None:
        """Record one MIDI event, timestamped inside the lock by default.

        Callers replaying a timeline they already own (the test suites, an
        imported performance) pass `when`; live callers leave it None. Sampling
        the clock *before* queueing at the lock is what put events into the
        buffers out of order: two rtmidi callback threads (the hardware port and
        the always-on virtual one) each read the clock, then whichever loses the
        race appends the older timestamp last. That breaks `snapshot()`'s
        oldest-first contract and can make `span()` -- and the duration in the
        export notification -- negative.
        """
        kind = status & 0xF0
        if kind not in (NOTE_ON, NOTE_OFF, CONTROL_CHANGE, PITCH_BEND):
            return
        with self._lock:
            if when is None:
                when = time.monotonic()
            self.session.append(when, status, d1, d2)
            if self.recording:
                self.take.append(when, status, d1, d2)

    # ------------------------------------------------------------ transport
    def start(self) -> None:
        with self._lock:
            self.take.clear()
            self.recording = True
            self.started_at = time.monotonic()
            self.stopped_at = None

    def stop(self) -> None:
        with self._lock:
            self.recording = False
            self.stopped_at = time.monotonic()

    def discard(self) -> None:
        with self._lock:
            self.take.clear()
            self.recording = False
            self.started_at = None
            self.stopped_at = None

    # -------------------------------------------------------------- inspection
    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at if not self.recording and self.stopped_at else time.monotonic()
        return max(0.0, end - self.started_at)

    def snapshot(self, which: str = "take") -> list:
        """The take or the session as a list of tuples, oldest first.

        Only the four array copies happen under `_lock`; the tuples are boxed
        after it is released. That lock is the one the rtmidi callback thread
        needs in `handle()`, and holding it for 400,000 tuples stalled MIDI for
        52-59 ms per export -- an audible hole and late notes.
        """
        with self._lock:
            columns = (self.take if which == "take" else self.session).columns()
        return rows(columns)

    def stats(self) -> RecorderStats:
        """Return `(take_notes, take_seconds, session_notes)` under `_lock`.

        A `RecorderStats` named tuple, cheap enough for the UI to poll: the note
        counts are maintained by `EventRing.append()` and the take span is read
        off the buffer's ends, so nobody has to walk (or trip over an eviction
        from) the live buffers while the MIDI thread appends to them.
        """
        with self._lock:
            return RecorderStats(self.take.notes, max(0.0, self.take.span()),
                                 self.session.notes)

    @staticmethod
    def note_count(events: Iterable) -> int:
        return sum(1 for _t, s, _d1, d2 in events if _is_note_on(s, d2))

    @staticmethod
    def duration(events) -> float:
        """Seconds from the first event to the last, never negative.

        A caller-supplied timestamp is not ordered by the lock, so two callback
        threads can still leave a snapshot whose ends are the wrong way round;
        an unclamped span reached the user as a negative length in the WAV export
        notification. `stats()` clamps the same way.
        """
        if not events:
            return 0.0
        return max(0.0, events[-1][0] - events[0][0])


# --------------------------------------------------------------------- export
def default_filename(prefix: str = "JustPiano", ext: str = "mid") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"


def _normalise(events):
    """Trim leading silence and make sure no note is left hanging."""
    if not events:
        return []
    # Sort up front: the time origin and the end-of-take markers below are all
    # positional, so they must not be derived from an unordered caller list.
    events = sorted(events, key=lambda e: e[0])
    t0 = events[0][0]
    out = [(round(t - t0, 6), s, d1, d2) for (t, s, d1, d2) in events]

    held: dict[tuple[int, int], None] = {}
    pedal_down: set[int] = set()
    for _t, s, d1, d2 in out:
        kind, chan = s & 0xF0, s & 0x0F
        if kind == NOTE_ON and d2 > 0:
            held[(chan, d1)] = None
        elif kind == NOTE_OFF or (kind == NOTE_ON and d2 == 0):
            held.pop((chan, d1), None)
        elif kind == CONTROL_CHANGE and d1 == 64:
            if d2 >= 64:
                pedal_down.add(chan)
            else:
                pedal_down.discard(chan)

    if held:
        end = out[-1][0] + 0.25
        for chan, note in held:
            out.append((end, NOTE_OFF | chan, note, 0))
    # Sustain pedal up at the very end so players don't get an endless blur --
    # on whichever channel(s) actually left it down.
    if pedal_down:
        end = out[-1][0] + 0.05
        for chan in sorted(pedal_down):
            out.append((end, CONTROL_CHANGE | chan, 64, 0))
    out.sort(key=lambda e: e[0])
    return out


def export_midi(events, path: str, bpm: float = EXPORT_BPM) -> str:
    """Write events to a Standard MIDI File (format 0)."""
    import mido

    events = _normalise(events)
    mid = mido.MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name="Just Piano Performance", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.Message("program_change", program=0, channel=0, time=0))

    ticks_per_second = TICKS_PER_BEAT * bpm / 60.0
    prev_tick = 0
    for t, status, d1, d2 in events:
        tick = int(round(t * ticks_per_second))
        delta = max(0, tick - prev_tick)
        prev_tick = tick
        kind, chan = status & 0xF0, status & 0x0F
        try:
            if kind == NOTE_ON and d2 > 0:
                msg = mido.Message("note_on", channel=chan, note=d1, velocity=d2, time=delta)
            elif kind in (NOTE_OFF,) or (kind == NOTE_ON and d2 == 0):
                msg = mido.Message("note_off", channel=chan, note=d1, velocity=0, time=delta)
            elif kind == CONTROL_CHANGE:
                msg = mido.Message("control_change", channel=chan, control=d1,
                                   value=d2, time=delta)
            elif kind == PITCH_BEND:
                value = ((d2 << 7) | d1) - 8192
                msg = mido.Message("pitchwheel", channel=chan, pitch=value, time=delta)
            else:
                prev_tick = tick - delta
                continue
        except Exception:
            prev_tick = tick - delta
            continue
        track.append(msg)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    mid.save(path)
    return path


def export_wav(events, bank, path: str, samplerate: int = 44100, volume: float = 0.75,
               reverb: str = "room", velocity_curve: str = "normal",
               tail: float = 3.0, progress=None) -> str:
    """Re-render a performance through the piano engine and write a WAV file.

    Raises `ValueError` with a user-readable message when there is nothing to
    render or when `bank` has not finished building: callers are expected to
    show it rather than accept a silently incomplete render.
    """
    from .samplebank import to_pcm16
    from .synth import AudioEngine

    events = _normalise(events)
    if not events:
        raise ValueError("nothing to render")
    if not getattr(bank, "ready", True):
        # AudioEngine.note_on() drops notes whose samples are not rendered yet,
        # so rendering now would silently omit part of the performance.
        raise ValueError("The piano samples are still being built - "
                         "try the export again in a moment.")

    engine = AudioEngine(bank, samplerate=samplerate, volume=volume,
                         reverb_preset=reverb, velocity_curve=velocity_curve)

    total = int((events[-1][0] + tail) * samplerate)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    # Render to a sibling and publish it with one rename, the way
    # `config.save()` does. Writing in place meant a raise from render() left a
    # short but perfectly playable RIFF file sitting at the name the user chose,
    # and truncated whatever they had picked in the save dialog before the first
    # block was even rendered.
    part = path + ".part"
    try:
        with wave.open(part, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(samplerate)

            idx = 0
            pos = 0
            n_events = len(events)
            while pos < total:
                while idx < n_events and int(events[idx][0] * samplerate) <= pos:
                    _dispatch(engine, events[idx])
                    idx += 1
                next_at = int(events[idx][0] * samplerate) if idx < n_events else total
                n = min(1024, max(1, next_at - pos), total - pos)
                block = engine.render(n)
                pcm = to_pcm16(block, "<i2")     # RIFF wants little-endian
                wav.writeframes(pcm.tobytes())
                pos += n
                if progress and (pos // samplerate) != ((pos - n) // samplerate):
                    progress(min(1.0, pos / total))
    except BaseException:
        # Includes the KeyboardInterrupt/SystemExit of a quit mid-export: half a
        # render is never worth leaving on disk under either name.
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    os.replace(part, path)
    return path


def _dispatch(engine, event) -> None:
    _t, status, d1, d2 = event
    kind = status & 0xF0
    if kind == NOTE_ON and d2 > 0:
        engine.note_on(d1, d2)
    elif kind == NOTE_OFF or (kind == NOTE_ON and d2 == 0):
        engine.note_off(d1)
    elif kind == CONTROL_CHANGE:
        engine.control_change(d1, d2)
