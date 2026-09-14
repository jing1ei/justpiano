"""
Real-time polyphonic piano engine.

Voices are sample-playback based (see `samplebank`), mixed with numpy inside the
PortAudio callback. Two velocity layers are cross-faded per note, notes are
panned across the stereo field by pitch, every strike is varied a little against
the one before it (`strike_variation`), and a vectorised Freeverb adds room.

Supported MIDI behaviour: note on/off with velocity, re-strike, sustain pedal
(CC64), sostenuto (CC66), soft pedal (CC67), all-sound-off (CC120), reset-all-
controllers (CC121) and all-notes-off (CC123).

Threading model. Three kinds of thread meet in here, and only one of them has a
deadline, so the state is split by owner rather than protected as a whole:

  * the **audio thread** (PortAudio's callback) owns `_voices`, the mix list, and
    every DSP object it feeds -- the reverb, the mute ramp, the release curves;
  * the **MIDI and UI threads** own `_notes`, the note -> voices registry every
    keyboard and pedal message works through, under `_lock`;
  * `Voice` flags (`releasing`, `pending`, `sostenuto`) travel from the second
    group to the first as single attribute stores, and `Voice.dead` travels back
    the same way. Nothing needs a lock to publish one word.

The only lock the callback ever takes is `_evt_lock`, and it is only ever held
for pointer swaps -- a list handed over, three flags read and cleared -- by every
thread that takes it. `_lock` can be held for as long as a MIDI burst needs and
the callback will not notice, which is the point: an RLock is unfair, so sharing
one with an unthrottled event stream cost the stream whole blocks.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from . import tone
from .resonance import Resonance
from .reverb import PRESETS as REVERB_PRESETS, Reverb
from .samplebank import SampleBank

MAX_VOICES = 64        # soft limit: excess voices are quickly faded out
HARD_MAX_VOICES = 128  # never let the mix list grow past this
RELEASE_TAU = 0.16       # damper fall time

#: Reference levels of the recorded mechanical noises, at a `key_noise` of 1.0 --
#: which is where a close pair of microphones over the strings puts them, and
#: which is a good deal louder than anyone wants to play under. What the app
#: actually uses is these times `AudioEngine.key_noise`, whose default is a
#: quarter of this; see `config.DEFAULTS["key_noise"]`.
RELEASE_NOISE = 0.45
PEDAL_NOISE = 0.5
FAST_RELEASE_TAU = 0.03  # re-strike / voice steal
INV_INT16 = np.float32(1.0 / 32768.0)

#: Release time constant for an "immediate" cut (CC120, Panic, `restart()`).
#: Dropping the voices instead was a step in the waveform: measured 0.343 at the
#: block boundary for a five-note chord at volume 1.0, and 0.92 -- a full-scale
#: edge -- at 1.5, against 0.007 for the ordinary CC123 release. 1.2 ms takes the
#: same edge to 0.007 and the whole cut is over in 10 ms (8.5 tau to the 2e-4
#: retirement floor), i.e. inside one block at every latency setting the menu
#: offers, so "immediate" still means immediate.
DECLICK_TAU = 0.0012

#: Mute fade, in seconds. A hard cut is a step in the waveform, i.e. a click,
#: and at full scale a loud one; 8 ms (353 frames at 44.1 kHz) is two orders of
#: magnitude longer than the ~0.1 ms edge that makes one audible, and still
#: short enough that the mute feels instant. It spans several blocks at the
#: lowest latency setting (128 frames = 2.9 ms), which is why the ramp carries
#: its position across calls instead of assuming one block is enough.
MUTE_RAMP = 0.008

#: Slack, in seconds, added to `Reverb.tail_frames` before `render()` stops
#: calling the reverb at all. The tail bound is already an upper bound on a
#: quantity that has decayed to -180 dBFS; a quarter of a second on top of it
#: costs 3 % of a core for a quarter of a second, once, per silence.
REVERB_IDLE_MARGIN = 0.25

#: Distinct release envelopes `_release_curve` will memoise before it starts
#: over. Live playback needs four (one per release time at one block size); the
#: ceiling is for the offline renderer, whose block length changes with every
#: event -- 64 curves of at most 1024 float32 is 256 KiB at the very worst.
RELEASE_CACHE_LIMIT = 64

VELOCITY_CURVES = {
    "soft": 0.85,
    "normal": 1.30,
    "hard": 1.90,
}

# ------------------------------------------------------------ strike variation
# Every strike of one key used to read the *same two buffers* from the same
# offset at the same gain, so a repeated note was bit-identical to the one
# before it -- the machine-gun tell, and the thing that most reliably gives a
# sampled or modelled piano away. No two hammer blows on a real instrument
# agree: the hammer arrives at a slightly different speed, at a slightly
# different point, on a string that has not finished moving.
#
# What follows is playback-side only. Rendering variants per key would cost
# another 44.6 MiB and a full build each, and buy little the four numbers below
# do not:
#
#   detune   the important one. Far too small to hear as pitch -- 1.1 cents is a
#            fifth of the spread already inside one unison choir -- it does its
#            work by *decorrelating* one strike from the next, so repeats stop
#            cancelling and reinforcing at identical phase.
#   gain     hammer speed the player did not ask for.
#   bright   shifts the soft/hard crossfade, so the variation is timbral and not
#            just a level wobble -- the two layers differ spectrally, which is
#            the whole reason they exist.
#   pan      the three strings of one key are not a point source.
#
#: Amounts at `strike_variation` = 1.0; each is applied as +- this.
_JITTER_DETUNE_CT = 1.1     # cents of playback detune
_JITTER_GAIN_DB = 0.35      # dB
_JITTER_BRIGHT = 0.035      # shift of the soft/hard crossfade weight
_JITTER_PAN = 0.014         # pan position

#: Drawn once, at import, from a fixed seed: the variation has to be *arbitrary*,
#: not *random*, or an exported WAV would differ from the take the player heard
#: and two runs of the test suite would disagree. 256 rows is far longer than any
#: phrase in which the ear could notice the sequence coming round again.
_JITTER = np.random.default_rng(0x11A2).uniform(-1.0, 1.0, size=(256, 4))
_JITTER_MASK = _JITTER.shape[0] - 1

#: Fractional read positions for one block, grown on demand. Live playback asks
#: for one size for ever; the offline renderer's block length changes with every
#: event, so this follows the largest it has seen rather than assuming one.
_READ_RAMP = np.arange(1024, dtype=np.float64)


def _read_ramp(frames: int) -> np.ndarray:
    """`arange(frames)` without allocating one per block."""
    global _READ_RAMP
    if frames > _READ_RAMP.size:
        _READ_RAMP = np.arange(frames, dtype=np.float64)
    return _READ_RAMP


def _hermite(src, im1, i0, frac):
    """Catmull-Rom read of `src` at `i0 + frac`, as float32.

    Four points, not two. Linear interpolation was tried first and is a third of
    the cost, but it is a lowpass whose corner moves with the fractional part, so
    a detuned voice gets its top octave attenuated by an amount that sweeps as
    the read walks -- measured against an exact (windowed-sinc) resample of a
    rendered C7, linear left an error only 41 dB under the note's own 2-5 kHz
    content, modulated at tens of Hz. That is exactly the kind of artefact this
    whole feature exists to remove. Catmull-Rom puts the same error 63 dB down,
    for 1.4 ms per block against 0.5 at the 64-voice cap (budget 5.8 ms).
    """
    a = src[im1].astype(np.float32)
    b = src[i0].astype(np.float32)
    c = src[i0 + 1].astype(np.float32)
    d = src[i0 + 2].astype(np.float32)
    c1 = (c - a) * np.float32(0.5)
    c2 = a - b * np.float32(2.5) + c * np.float32(2.0) - d * np.float32(0.5)
    c3 = (d - a) * np.float32(0.5) + (b - c) * np.float32(1.5)
    y = c3 * frac
    y += c2
    y *= frac
    y += c1
    y *= frac
    y += b
    return y


class Voice:
    """One sounding note. Created on the MIDI thread, mixed on the audio thread.

    `fpos`, `rel_gain` and `dead` belong to the audio thread once the voice has
    been handed over; the flags above them are written by whichever thread the
    keyboard or a pedal arrives on. Both directions are single attribute stores,
    so neither needs a lock -- and `dead` is the one word that travels back, so
    that the note registry can forget a voice that has retired.
    """

    __slots__ = ("soft", "hard", "ws", "wh", "fpos", "step", "gl", "gr",
                 "stereo", "releasing", "rel_gain", "rel_tau", "pending",
                 "sostenuto", "started", "dead")

    def __init__(self, soft, hard, ws, wh, gl, gr, step=1.0, stereo=False):
        self.soft = soft
        self.hard = hard
        self.ws = np.float32(ws)
        self.wh = np.float32(wh)
        #: Read position, in source samples, and how far it advances per output
        #: frame. `step` is exactly 1.0 unless this strike was detuned, and the
        #: mixer keeps a whole-sample fast path for that case.
        self.fpos = 0.0
        self.step = float(step)
        #: Whether `soft`/`hard` are (n, 2) recordings or (n,) rendered mono.
        #: A recording already carries its own stereo image -- the microphones
        #: were two feet apart over the strings -- so panning one by pitch would
        #: be squeezing a picture that is already there. `gl`/`gr` stay, because
        #: the strike variation still nudges the balance.
        self.stereo = bool(stereo)
        self.gl = np.float32(gl)
        self.gr = np.float32(gr)
        self.releasing = False
        self.rel_gain = 1.0
        self.rel_tau = RELEASE_TAU
        self.pending = False      # key released while sustain pedal is down
        self.sostenuto = False
        self.started = time.monotonic()
        self.dead = False         # retired: the mixer will never read it again

    def release(self, tau: float = RELEASE_TAU) -> None:
        if not self.releasing:
            self.releasing = True
            self.rel_tau = tau
        else:
            self.rel_tau = min(self.rel_tau, tau)


class AudioEngine:
    def __init__(self, bank: SampleBank, samplerate: int = tone.SAMPLE_RATE,
                 blocksize: int = 256, volume: float = 0.75,
                 reverb_preset: str = "room", velocity_curve: str = "normal",
                 strike_variation: float = 1.0,
                 resonance: float = 1.0, key_noise: float = 0.25) -> None:
        self.bank = bank
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.volume = float(volume)
        self.velocity_curve = velocity_curve
        #: Depth of the per-strike variation, 0.0 = off (see `_JITTER`). Read on
        #: the MIDI thread, written by the UI thread; one float, no lock.
        self.strike_variation = max(0.0, float(strike_variation))
        #: The undamped strings. Costs nothing while the damper pedal is up: its
        #: `process` returns on a single test until a damper actually lifts.
        self.resonance = Resonance(samplerate)
        self.resonance.depth = max(0.0, float(resonance))
        #: How loud the recorded key and pedal noise sits. Read on the MIDI
        #: thread, written by the UI thread; one float, no lock.
        self.key_noise = max(0.0, float(key_noise))
        #: Which `_JITTER` row the next strike starts from. See `note_on`.
        self._strike_ix = 0
        self.reverb = Reverb(reverb_preset)

        self._lock = threading.RLock()
        #: Guards the pending *control* flags below (preset, reset, mute), which
        #: are rare and user-driven. `render()` only takes it on the blocks where
        #: one of them is actually set -- it checks first, and a single attribute
        #: read needs nothing -- so a MIDI flood cannot convoy the callback out of
        #: a lock it is not asking for.
        self._evt_lock = threading.Lock()
        #: The mix list: the audio thread's own, drained from `_pending` at the
        #: top of every block and never touched by another thread.
        self._voices: list[Voice] = []
        #: Voices `note_on` has built and the callback has not adopted yet. A
        #: deque because `append` and `popleft` are atomic: the note path is the
        #: one handover that happens at MIDI rate, and it is the one that must not
        #: put a lock in front of the callback at all.
        self._pending: deque[Voice] = deque()
        #: Voices no key owns -- damper and pedal noises. Held only so that an
        #: immediate silence can reach them; they retire on their own otherwise.
        self._oneshots: list[Voice] = []
        #: note -> voices registry, owned by the MIDI/UI threads under `_lock`.
        #: Every keyboard and pedal message reaches a voice through here, so the
        #: callback does not have to share the structure it mixes from. Retired
        #: voices are dropped lazily (see `_by_note`, `active_voices`).
        self._notes: dict[int, list[Voice]] = {}
        self.sustain = False
        self.soft_pedal = False
        self.sostenuto_down = False  # pedal position: capture only on its edge
        self._rel_cache: dict[tuple[int, float], np.ndarray] = {}
        # Reverb state belongs to the audio thread; other threads only queue
        # requests here and `render()` applies them (see `set_reverb`).
        self._pending_reverb: Optional[str] = None
        self._pending_reverb_reset = False
        #: A consumed reverb reset that is waiting for the voices it was asked
        #: for to finish fading (audio thread only); see `render`.
        self._reset_when_quiet = False
        #: Consecutive frames rendered with no voice at all. The reverb is a
        #: recursion on its own output, so it keeps costing 3.3 % of a core for
        #: as long as the app is running unless somebody notices that silence in
        #: means silence out. `_reverb_idle_limit` is where "silence out" becomes
        #: provable rather than likely; see `Reverb.tail_frames`.
        self._idle_frames = 0
        self._reverb_idle_limit = self._idle_limit()
        self._reverb_settled = False
        #: Published mute state: written by `set_muted`, read by `note_on` (the
        #: rtmidi thread) and by the tray's menu/icon. The *gain* it asks for is
        #: the audio thread's business, below.
        self.muted = False
        self._pending_mute: Optional[bool] = None
        # Audio-thread state: where the fade is now and where it is heading.
        # 1.0 = fully audible, 0.0 = fully muted.
        self._mute_gain = 1.0
        self._mute_target = 1.0
        #: Latch: the mute fade has landed and the tank behind it was cleared.
        #: Set on the transition into silence, cleared while the ramp moves, so
        #: the clear happens exactly once per mute (see `render`).
        self._mute_silenced = False

        self.stream = None
        self.device = None
        self.error: Optional[str] = None

    # ------------------------------------------------------------- stream I/O
    def start(self, device=None) -> bool:
        """Open the output stream. Returns True on success."""
        import sounddevice as sd  # imported lazily: keeps DSP testable headless

        self.stop()
        self.device = device
        try:
            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                channels=2,
                dtype="float32",
                device=device,
                latency="low",
                callback=self._callback,
            )
            self.stream.start()
            self.error = None
            return True
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error = str(exc)
            if self.stream is not None:
                # sounddevice has no finalizer: without close() the already
                # opened PortAudio stream would never be released.
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            return False

    def stop(self) -> None:
        """Close the output stream and forget everything that was sounding.

        PortAudio joins the callback thread in `close()`, so past this point the
        mix list has no other owner and dropping it outright is safe -- and it is
        also the one cut that cannot click, because nothing is being played into
        a device any more. Every caller pairs this with `all_notes_off()`; doing
        it here as well is what keeps a voice from surviving a stop and resuming
        mid-sample (or, in `tray._swap_bank`, from still holding a buffer of the
        bank that is being replaced) when the stream comes back.
        """
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self._forget_voices()

    def _forget_voices(self) -> None:
        """Drop every voice, queued or mixing. Never call this with a live stream."""
        self.resonance.reset()
        with self._lock:
            self._oneshots = []
            for voices in self._notes.values():
                for voice in voices:
                    voice.dead = True
            self._notes.clear()
        while True:
            try:
                self._pending.popleft().dead = True
            except IndexError:
                break
        self._voices.clear()

    def restart(self) -> bool:
        self.stop()  # silence the callback before touching any DSP state
        self.all_notes_off(immediate=True)
        self.reverb.reset()
        return self.start(self.device)

    @property
    def latency_ms(self) -> float:
        if self.stream is not None:
            try:
                return float(self.stream.latency) * 1000.0
            except Exception:
                pass
        return self.blocksize / self.samplerate * 1000.0

    # ---------------------------------------------------------------- control
    def set_volume(self, value: float) -> None:
        self.volume = max(0.0, min(1.5, float(value)))

    def set_reverb(self, preset: str) -> None:
        """Queue a reverb preset; the audio thread switches it in `render()`.

        The coefficients and the delay lines are only ever touched by the thread
        that owns them, so a preset change cannot tear a block in half. The
        `Reverb.name` label is published eagerly so that it never lags behind the
        request even if no block is rendered (there are no menu readers -- the
        tray checkmarks come from `settings["reverb"]`; the headless suites in
        `tools/` are the readers). It is therefore written from here *and* from
        `Reverb.set_preset()` on the audio thread, so if two switches land in the
        same block the label can trail the newest request by one block; only the
        coefficients, applied strictly in order, are authoritative.
        """
        with self._evt_lock:
            self._pending_reverb = preset
            self.reverb.name = preset if preset in REVERB_PRESETS else "room"

    def reset_reverb(self) -> None:
        """Thread-safely drop the reverb tails (Panic).

        The main thread must never zero the numpy delay lines while the callback
        is halfway through `Reverb.process()`, so this only raises a flag that
        `render()` consumes at the top of the next block.
        """
        with self._evt_lock:
            self._pending_reverb_reset = True

    def set_velocity_curve(self, name: str) -> None:
        self.velocity_curve = name if name in VELOCITY_CURVES else "normal"

    def set_resonance(self, depth: float) -> None:
        """How loud the sympathetic strings are; 0.0 silences the bank."""
        self.resonance.depth = max(0.0, float(depth))

    def set_key_noise(self, amount: float) -> None:
        """How loud the action is; 0.0 silences it. Takes effect on the next key."""
        self.key_noise = max(0.0, float(amount))

    def set_strike_variation(self, amount: float) -> None:
        """How far one strike may differ from the next; 0.0 turns it off.

        Takes effect on the next note. Voices already sounding keep the variation
        they were built with, which is the only behaviour that does not click.
        """
        self.strike_variation = max(0.0, float(amount))

    def set_muted(self, muted: bool) -> None:
        """Queue a mute change; the audio thread fades into it in `render()`.

        Exactly the handover `set_reverb` uses, and for the same reason: the
        master gain is the audio thread's state, so this only leaves a request
        behind - the callback picks it up with the pointer swap it already does
        under `_evt_lock`, and no block can be torn in half. `muted` itself is
        published eagerly, because `note_on` (rtmidi thread) and the tray's
        checkmark, panel button and menu bar icon read it and must not lag a
        block behind the click.

        This is an *output-stage* mute. MIDI keeps arriving, the recorder keeps
        recording and the keys keep lighting up: a take can be captured in
        silence and exported afterwards.
        """
        with self._evt_lock:
            self.muted = bool(muted)
            self._pending_mute = bool(muted)

    @property
    def mute_gain(self) -> float:
        """Where the mute fade currently sits: 1.0 audible, 0.0 silent.

        The audio thread's own state, published for the tests; a reader on
        another thread only ever sees a whole float, never half a ramp.
        """
        return self._mute_gain

    @property
    def _by_note(self) -> dict[int, list[Voice]]:
        """The note registry as it stands, retired voices dropped.

        The registry itself is pruned lazily (a key's list is tidied the next
        time that key is struck), because the thread that retires a voice is the
        one thread that must not take `_lock` to do it -- it publishes
        `Voice.dead` instead. Reading it through here is what makes that
        invisible: a note whose voices have all died is simply not in the dict.
        """
        with self._lock:
            live = {note: [v for v in voices if not v.dead]
                    for note, voices in self._notes.items()}
        return {note: voices for note, voices in live.items() if voices}

    @property
    def active_voices(self) -> int:
        """Voices a key or a pedal is still responsible for.

        Counted from the registry rather than from the mix list, so it answers
        the same before and after the callback has adopted a note -- and so a
        voice that is only finishing its declick fade (CC120, Panic) no longer
        counts, which is what "all sound off" means to everything upstream.
        """
        with self._lock:
            return sum(1 for voices in self._notes.values()
                       for v in voices if not v.dead)

    # ------------------------------------------------------------ MIDI events
    def note_on(self, note: int, velocity: int) -> None:
        """Strike a key. MIDI thread: everything expensive happens outside a lock.

        The voice is built, panned, gain-staged and prefaulted here, and only
        then handed to the callback through `_pending`. `_evt_lock` covers the
        handover itself -- a length, an append -- and nothing else, so the
        callback's worst case does not depend on how fast the events arrive.
        """
        if velocity <= 0:
            self.note_off(note)
            return
        if self.muted:
            # Muted: allocate nothing. Nobody can hear this note, and a voice
            # kept "just in case" is precisely what would resurrect it (or leave
            # it stuck) on unmute - so the predictable model is that muting is a
            # dead output stage. The key still lights and still records; only the
            # audio voice is skipped, which is also why unmuting is instantly
            # clean instead of dumping a pile of held notes into the mix.
            return
        note = int(note)
        if not tone.NOTE_MIN <= note <= tone.NOTE_MAX:
            # Rejected, not clamped: an 88-key piano has no key here, and
            # folding one onto A0 or C8 invented a note the player never played
            # (note_on(19) and note_on(20) both sounded A0, and the matching
            # note_off(19) then released a genuinely held A0).
            return
        exponent = VELOCITY_CURVES.get(self.velocity_curve, 1.30)
        got = self.bank.note_mix(note, float(velocity), exponent)
        if got is None:
            return  # this key is not ready yet
        soft, hard, ws, wh, base_step, amp = got
        # Off the audio thread, before the callback can be the one to touch
        # these pages: the samples are a mmap the OS is free to reclaim, and the
        # fault it takes to bring them back does not fit in a block (measured
        # 7.9 ms of a 5.8 ms budget, 48 major faults, on the first block of a
        # note played after an idle period).
        self.bank.prefault(note)

        # Una corda: the hammer meets fewer strings, so the blow is both softer
        # and duller. The gain is `0.72 ** exponent` rather than 0.72 because
        # the velocity curve used to be applied *after* the pedal scaled the
        # velocity, and this keeps that exactly -- while `wh` biases the mix
        # towards the softer layer, which is the duller half of the pair
        # whichever kind of bank is sounding.
        if self.soft_pedal:
            amp *= 0.72 ** exponent
            wh *= 0.6
            ws = 1.0 - wh

        # Gentle pitch-dependent stereo spread, like sitting at the keyboard.
        pan = max(-0.34, min(0.34, (note - 62) / 48.0 * 0.34))

        # No two hammer blows agree (see `_JITTER` above). The row is picked from
        # both a strike counter and the key, so a trill alternates rows and a
        # repeated note never draws the same one twice running. `_strike_ix` is
        # incremented without a lock on purpose: two threads racing it lose a
        # count and reuse a row, which is a variation that repeats once -- the
        # cheapest possible failure, and not worth a lock in front of the note
        # path.
        step = base_step
        var = self.strike_variation
        if var > 0.0:
            k = self._strike_ix
            self._strike_ix = k + 1
            jd, jg, jb, jp = _JITTER[(k * 7 + note * 13) & _JITTER_MASK]
            step = base_step * 2.0 ** (jd * _JITTER_DETUNE_CT * var / 1200.0)
            amp *= 10.0 ** (jg * _JITTER_GAIN_DB * var / 20.0)
            wh = min(1.0, max(0.0, wh + jb * _JITTER_BRIGHT * var))
            ws = 1.0 - wh
            pan = max(-0.5, min(0.5, pan + jp * _JITTER_PAN * var))
        theta = (pan + 1.0) * 0.25 * math.pi
        gl = amp * math.cos(theta) * 1.414
        gr = amp * math.sin(theta) * 1.414

        voice = Voice(soft, hard, ws * INV_INT16, wh * INV_INT16, gl, gr, step,
                      self.bank.stereo)

        with self._lock:
            # Re-strike: the voices this key already has step aside. The list is
            # tidied of retired voices on the way past, which is the whole of the
            # registry's book-keeping.
            live = [v for v in self._notes.get(note, ()) if not v.dead]
            for old in live:
                old.release(FAST_RELEASE_TAU)
            live.append(voice)
            self._notes[note] = live
            if self.sustain:
                self._republish_dampers()

        pending = self._pending
        while len(pending) >= HARD_MAX_VOICES:
            # A stuck controller can outrun the callback: everything past the
            # hard cap is going to be dropped at the splice anyway, so give up on
            # the oldest here rather than let the queue grow without bound.
            try:
                pending.popleft().dead = True
            except IndexError:      # the callback drained it first
                break
        pending.append(voice)       # atomic; no lock in front of the callback

        self._enforce_polyphony(len(self._voices) + len(pending))

    def _cut_oneshots(self, tau: float) -> None:
        """Fade the keyless voices out too. Caller holds `_lock`.

        No key and no pedal owns a damper thud or a pedal clunk, which is what
        keeps them out of `_notes` -- but "all sound off" means all of it, and a
        six-second pedal recording ringing on through a panic is exactly the kind
        of thing that only shows up in front of an audience.
        """
        for voice in self._oneshots:
            if not voice.dead:
                voice.release(tau)
        self._oneshots = []

    def _oneshot(self, buf, gain: float) -> None:
        """Sound a recording that belongs to no key: a damper, or the pedal.

        These are the mechanical half of a piano -- felt landing on a string, the
        whole damper rail lifting -- and they are the part a model has the least
        chance with, because none of it is a vibrating string. They are ordinary
        voices with one layer and no pitch shift, so they take the mixer's cheap
        path, but they are kept out of the note registry: no key owns them and no
        pedal should reach them. `_oneshots` is how a panic still can.
        """
        # The instrument gets a say as well as the user: an upright's action is
        # genuinely busier than a grand's, and its recordings were made in a
        # living room rather than over the strings.
        gain *= self.key_noise * getattr(self.bank, "noise_scale", 1.0)
        if buf is None or self.muted or gain <= 0.0:
            return
        voice = Voice(buf, buf, np.float32(gain) * INV_INT16, np.float32(0.0),
                      1.0, 1.0, 1.0, getattr(self.bank, "stereo", False))
        with self._lock:
            self._oneshots = [v for v in self._oneshots if not v.dead]
            self._oneshots.append(voice)
        self._pending.append(voice)

    def note_off(self, note: int) -> None:
        note = int(note)
        if not tone.NOTE_MIN <= note <= tone.NOTE_MAX:
            return      # never a key of this piano; see `note_on`
        damped = False
        with self._lock:
            for voice in self._notes.get(note, ()):
                if voice.dead or voice.releasing:
                    continue
                if self.sustain or voice.sostenuto:
                    voice.pending = True
                else:
                    voice.release()
                    damped = True
            if self.sustain:
                self._republish_dampers()
        # Only when a damper actually lands: a key let go under the pedal makes
        # no sound at all, which is the whole point of the pedal.
        if damped:
            self._oneshot(self.bank.release(note), RELEASE_NOISE)

    def control_change(self, controller: int, value: int) -> None:
        if controller == 64:      # sustain / damper
            self.set_sustain(value >= 64)
        elif controller == 66:    # sostenuto
            self._set_sostenuto(value >= 64)
        elif controller == 67:    # una corda
            self.soft_pedal = value >= 64
        elif controller == 120:    # All Sound Off: silence now, damper ignored
            self._release_all(immediate=True)
        elif controller == 121:    # Reset All Controllers: the pedals go up
            self.reset_controllers()
        elif controller == 123:    # All Notes Off: keys up, damper honoured
            self._release_all(immediate=False)

    def _live_voices(self) -> list[Voice]:
        """Every voice a key or pedal can still reach (caller holds `_lock`).

        Taken from the registry, not from the mix list: the mix list is compacted
        in place by the audio thread, and walking a list while another thread
        rewrites its slots is how a pedal silently misses a voice and leaves a
        note stuck.
        """
        return [v for voices in self._notes.values() for v in voices if not v.dead]

    def set_sustain(self, down: bool) -> None:
        if down != self.sustain:
            self._oneshot(self.bank.pedal_noise(down), PEDAL_NOISE)
        with self._lock:
            self.sustain = down
            if not down:
                for voice in self._live_voices():
                    if voice.pending and not voice.sostenuto:
                        voice.pending = False
                        voice.release()
            self._republish_dampers()

    def _republish_dampers(self) -> None:
        """Tell the resonance bank which strings are free to move.

        Caller holds `_lock`, because this reads the note registry. It is called
        from the pedal and from `note_on`/`note_off` rather than from the audio
        thread: the set only changes when a key or a pedal does, and the audio
        thread should not be walking a dict.
        """
        sounding = {note for note, voices in self._notes.items()
                    if any(not v.dead for v in voices)}
        self.resonance.set_open(self.sustain, sounding)

    def _set_sostenuto(self, down: bool) -> None:
        with self._lock:
            if down:
                if self.sostenuto_down:
                    # Continuous/repeating pedals resend CC66 while held down;
                    # re-running the capture would retroactively latch notes
                    # played *after* the press. Freeze the set at the edge.
                    return
                self.sostenuto_down = True
                for voice in self._live_voices():
                    if not voice.releasing and not voice.pending:
                        voice.sostenuto = True
            else:
                self.sostenuto_down = False
                for voice in self._live_voices():
                    if voice.sostenuto:
                        voice.sostenuto = False
                        if voice.pending and not self.sustain:
                            voice.pending = False
                            voice.release()

    def reset_controllers(self) -> None:
        """CC121: put every pedal back up without cutting the sounding notes.

        This -- not All Notes Off -- is the message that clears the latches, so
        a transport stop cannot leave `sustain` False under a held damper.
        """
        with self._lock:
            self.soft_pedal = False
            self._set_sostenuto(False)
            self.set_sustain(False)

    def _release_all(self, immediate: bool = False) -> None:
        """Let go of every key, leaving the pedal latches (hardware) alone.

        `immediate` is All Sound Off (CC120): cut the voices at once, damper or
        not -- and with them the undamped strings, because "all sound off" means
        all of it. The damper latch itself is deliberately left alone, so the
        next note republishes the state and the bank fills again from silence. Otherwise this is All Notes Off (CC123), which must behave exactly
        like releasing the keys by hand -- a held damper or sostenuto keeps the
        notes ringing until that pedal is lifted.

        "At once" is `DECLICK_TAU`, not a dropped voice list: a hard cut is a
        step in the waveform and at any usable volume an audible click. The keys
        are given up here and now -- the registry is emptied, so nothing upstream
        can see the notes any more -- and the voices themselves spend their last
        10 ms fading out on the audio thread.
        """
        with self._lock:
            if immediate:
                for voice in self._live_voices():
                    voice.release(DECLICK_TAU)
                self._notes.clear()
                self._cut_oneshots(DECLICK_TAU)
                self.resonance.silence()
                return
            for voice in self._live_voices():
                if voice.releasing:
                    continue
                if self.sustain or voice.sostenuto:
                    voice.pending = True
                else:
                    voice.release(0.08)
            # CC123 is "every key up", not "stop": under a held damper the notes
            # keep ringing, so the strings they are driving keep answering. Only
            # the note set changed, so the bank is republished, not silenced.
            if self.sustain:
                self._republish_dampers()

    def all_notes_off(self, immediate: bool = False) -> None:
        """Full panic: silence the keyboard *and* drop the pedal latches.

        Used by the tray's Panic item and by `restart()`. Incoming CC120/CC123
        deliberately do not come here -- they must not invert the physical
        pedal state (see `control_change` / `_release_all`).

        `immediate` is the `DECLICK_TAU` cut of `_release_all`, for the same
        reason: perceptually instant, but not a step in the waveform.
        """
        with self._lock:
            self.sustain = False
            self.soft_pedal = False
            self.sostenuto_down = False
            if immediate:
                for voice in self._live_voices():
                    voice.release(DECLICK_TAU)
                self._notes.clear()
            else:
                for voice in self._live_voices():
                    voice.pending = False
                    voice.sostenuto = False
                    voice.release(0.08)
            self._cut_oneshots(DECLICK_TAU if immediate else 0.08)
            # A panic drops the pedals, so nothing is undamped any more: the
            # sympathetic strings have to stop with the keyboard. Cutting the
            # bank outright rather than ramping it is safe where a voice is not
            # -- it is 14 dB down and, unlike a voice, it is *driven* by the mix
            # rather than replayed from it, so it is already at whatever the
            # last block left, not at full scale.
            self.resonance.silence()

    def _enforce_polyphony(self, count: int) -> None:
        """Steal voices down to the limits. MIDI thread, outside every lock.

        `count` is what `note_on` just counted: the callback's mix list plus the
        voices queued for it. The `sorted()` this needs is the reason the whole
        computation moved out here -- it used to run under the same lock the
        callback took to render, where an unthrottled event stream turned it into
        a 3.3 s worst-case block.
        """
        if count <= MAX_VOICES:
            return
        try:
            queued = list(self._pending)
        except RuntimeError:
            # Another strike landed mid-copy. Nothing to do about it and nothing
            # to fix: that strike is running this same computation behind us.
            queued = []
        # Steal the oldest already-releasing voices first, then the oldest held.
        pool = [v for v in list(self._voices) + queued if not v.dead]
        victims = sorted(pool, key=lambda v: (not v.releasing, v.started))
        for voice in victims[: count - MAX_VOICES]:
            voice.release(FAST_RELEASE_TAU)
        if count > HARD_MAX_VOICES:
            # Pathological input (e.g. a stuck controller spraying note_on): give
            # up on the oldest outright so the audio callback stays bounded. The
            # mixer reaps them at the next block; `dead` is the only word another
            # thread may write into its mix list.
            for voice in victims[: count - HARD_MAX_VOICES]:
                voice.dead = True

    # -------------------------------------------------------------- rendering
    def _release_curve(self, frames: int, tau: float) -> np.ndarray:
        """One release envelope, memoised. Audio thread only.

        Live playback asks for at most a handful of shapes -- the block size
        never changes, and there are four release times -- so this is warm after
        a few blocks and never allocates again. The offline WAV renderer is the
        one caller that does not have a fixed block size (`export_wav` cuts each
        block at the next event), so it can ask for up to a thousand lengths and
        was slowly filling this with curves nothing would ever want twice. The
        cache is dropped wholesale rather than evicted one entry at a time: it is
        pure memoisation, rebuilding a working set costs a few blocks of
        arithmetic, and a policy with a queue in it would be more moving parts on
        the one thread that must not have them.
        """
        key = (frames, round(tau, 4))
        curve = self._rel_cache.get(key)
        if curve is None:
            if len(self._rel_cache) >= RELEASE_CACHE_LIMIT:
                self._rel_cache.clear()
            n = np.arange(1, frames + 1, dtype=np.float32)
            curve = np.exp(-n / (tau * self.samplerate)).astype(np.float32)
            self._rel_cache[key] = curve
        return curve

    def _reap(self) -> None:
        """Compact `_voices` in place, dropping everything marked dead.

        Audio thread only. In place because the callback may not allocate: the
        list keeps its order, which is strike order, which is what lets the hard
        cap drop the oldest voices with a slice instead of a sort.
        """
        voices = self._voices
        kept = 0
        for voice in voices:
            if not voice.dead:
                voices[kept] = voice
                kept += 1
        del voices[kept:]

    def _idle_limit(self) -> int:
        """Frames of silence after which `render()` may stop calling the reverb.

        The reverb's own decay bound plus `REVERB_IDLE_MARGIN`. Recomputed
        whenever the preset changes, because Concert Hall rings for 14 s where
        Room rings for 5.7 -- guessing one number for both is exactly how a real
        tail gets clipped.
        """
        return self.reverb.tail_frames + int(REVERB_IDLE_MARGIN * self.samplerate)

    def _mute_env(self, frames: int) -> Optional[np.ndarray]:
        """Advance the mute fade by one block.

        Returns the per-sample master gain, or None when the gain is not moving
        - a block that is fully audible must not pay for a multiply by a flat
        array of ones. Audio thread only: `_mute_gain` is its state, and the
        value the last block ended on is where this one starts, so consecutive
        blocks join without a step.
        """
        gain, target = self._mute_gain, self._mute_target
        if gain == target:
            return None
        step = 1.0 / max(1.0, MUTE_RAMP * self.samplerate)
        env = np.arange(1, frames + 1, dtype=np.float32)
        env *= np.float32(step if target > gain else -step)
        env += np.float32(gain)
        np.clip(env, min(gain, target), max(gain, target), out=env)
        self._mute_gain = float(env[-1])
        return env

    def render(self, frames: int) -> np.ndarray:
        """Mix one block. The audio callback's whole world; see the module docs.

        `_evt_lock` is taken once, for as long as it takes to swap two list
        references and read three flags. Everything after that -- the mix, the
        reverb, the master stage -- runs on state no other thread touches, so the
        block's cost depends on the voices sounding and on nothing else.
        """
        if frames <= 0:
            # PortAudio has been seen to ask for nothing, and `render(0)` is also
            # reachable from the WAV exporter's block loop. Nothing to mix, and
            # the mute ramp must not be advanced by a zero-length step: `env[-1]`
            # on the empty envelope raised IndexError, which `_callback` then
            # swallowed into a block of silence.
            return np.zeros((0, 2), dtype=np.float32)
        out = np.zeros((frames, 2), dtype=np.float32)
        preset = None
        do_reset = False
        request = None
        if (self._pending_reverb is not None or self._pending_reverb_reset
                or self._pending_mute is not None):
            # Reverb mutations are marshalled here: this is the only thread
            # allowed to touch the delay lines. Mute arrives the same way. The
            # lock is what makes read-and-clear one step, so a request that lands
            # between the two cannot be dropped -- and it is only taken on the
            # rare block that has one, by a menu click or a hotkey.
            with self._evt_lock:
                preset, self._pending_reverb = self._pending_reverb, None
                do_reset, self._pending_reverb_reset = self._pending_reverb_reset, False
                request, self._pending_mute = self._pending_mute, None

        if request is not None:
            self._mute_target = 0.0 if request else 1.0
        env = self._mute_env(frames)
        silent = env is None and self._mute_gain == 0.0
        if env is not None:
            self._mute_silenced = False   # the ramp is moving again
        mixed = False       # did any voice put a sample into this block?
        if silent:
            # The fade has finished, so anything still allocated is inaudible.
            # Retire it here, on the thread that owns the mix: a muted app must
            # not keep summing 60 buffers into silence, and a voice that survived
            # is the one that would come back on unmute. The pedal latches are
            # deliberately left alone: CC64/CC66 keep arriving while muted and
            # must stay true to the hardware.
            for voice in self._voices:
                voice.dead = True
            self._voices.clear()
            while True:
                try:
                    self._pending.popleft().dead = True   # raced `set_muted`
                except IndexError:
                    break
            # The registry is *not* touched from here: marking the voices dead
            # is enough for it to read as empty (see `_by_note`), and the
            # callback taking `_lock` -- or mutating a dict a pedal message may be
            # walking -- is exactly what this design is for.
            if not self._mute_silenced:
                # ...nor may the reverb tail outlive the fade. Latched on the
                # *transition*, not on "there were voices": the last voice is
                # very often already gone by the time the fade lands (keys
                # released, or CC120), and that was exactly the case in which the
                # tank was left frozen mid-tail instead of cleared -- unmuting
                # inside the tail bound then replayed it from where it stopped.
                do_reset = True
                self._mute_silenced = True
        else:
            pending = self._pending
            if pending:
                # Adopt the voices `note_on` built, at most one cap's worth per
                # block so that a flood cannot stretch this loop. Strike order is
                # preserved, which is what lets the cap below drop the oldest with
                # a slice instead of another sort.
                voices = self._voices
                for _ in range(HARD_MAX_VOICES):
                    try:
                        voices.append(pending.popleft())
                    except IndexError:
                        break
                excess = len(voices) - HARD_MAX_VOICES
                if excess > 0:
                    del voices[:excess]
            reap = False
            for voice in self._voices:
                if voice.dead:      # stolen by `_enforce_polyphony`
                    reap = True
                    continue
                # Frames, not samples: `size` counts both channels of a stereo
                # recording and would let the reader run twice as far as the
                # buffer goes.
                #
                # And the *shorter* of the two layers, because they are not
                # always the same length. A pack whose velocity layers are laid
                # out independently -- the upright's are, its loud and soft
                # regions centring on different keys -- hands over two buffers
                # trimmed to two different lengths, and reading the shorter one
                # at the longer one's index is an IndexError inside the audio
                # callback. What that costs is a few milliseconds off the tail of
                # the longer layer, under a crossfade that is already blending
                # the two.
                length = min(len(voice.soft), len(voice.hard))
                step = voice.step
                if step == 1.0:
                    # Whole-sample fast path: `strike_variation` off, or a
                    # strike whose detune rounded to nothing. Two slices and two
                    # casts, which is what this loop cost before detune existed.
                    start = int(voice.fpos)
                    avail = min(frames, length - start)
                    if avail <= 0:
                        voice.dead = True
                        reap = True
                        continue
                    stop = start + avail
                    seg = voice.soft[start:stop].astype(np.float32)
                    seg *= voice.ws
                    if voice.wh:
                        seg += voice.hard[start:stop].astype(np.float32) * voice.wh
                    voice.fpos = float(stop)
                else:
                    # Detuned: read at a fractional rate. The last frame reads
                    # source index fpos + step*(avail-1) and the interpolation
                    # reaches two samples past it, so the bound is three samples
                    # short of the buffer, not zero.
                    avail = min(frames,
                                int((length - 3 - voice.fpos) / step) + 1)
                    if avail <= 0:
                        voice.dead = True
                        reap = True
                        continue
                    idx = _read_ramp(frames)[:avail] * step
                    idx += voice.fpos
                    i0 = idx.astype(np.int64)
                    frac = (idx - i0).astype(np.float32)
                    if voice.stereo:
                        # One column of fractions per channel, so the same
                        # gather feeds both: numpy indexes an (n, 2) source in
                        # one call, which is why stereo costs about twice the
                        # data but not twice the Python.
                        frac = frac[:, None]
                    im1 = i0 - 1
                    if voice.fpos < 1.0:
                        # Only ever true on a voice's first block.
                        np.maximum(im1, 0, out=im1)
                    seg = _hermite(voice.soft, im1, i0, frac)
                    seg *= voice.ws
                    if voice.wh:
                        # A one-shot (a damper, the pedal) has no second layer
                        # and a strike at the top or bottom of the velocity
                        # range has both weights on one of them.
                        other = _hermite(voice.hard, im1, i0, frac)
                        other *= voice.wh
                        seg += other
                    voice.fpos += step * avail

                if voice.releasing:
                    curve = self._release_curve(avail, voice.rel_tau)
                    # The curve is one column; a stereo segment needs it applied
                    # to both channels, which is a reshape and not a second
                    # curve -- the two channels are one voice being let go of.
                    seg *= curve[:, None] if voice.stereo else curve
                    seg *= np.float32(voice.rel_gain)
                    voice.rel_gain *= float(curve[-1])
                    if voice.rel_gain < 2e-4:
                        voice.dead = True
                        reap = True

                if voice.stereo:
                    seg[:, 0] *= voice.gl
                    seg[:, 1] *= voice.gr
                    out[:avail] += seg
                else:
                    out[:avail, 0] += seg * voice.gl
                    out[:avail, 1] += seg * voice.gr
                mixed = True

                if voice.fpos >= length - 1:
                    voice.dead = True
                    reap = True

            if reap:
                self._reap()

        # One int per block, on the thread that owns the voice list: how long the
        # mixer has been feeding the reverb nothing at all.
        if self._voices:
            self._idle_frames = 0
        else:
            self._idle_frames += frames

        # The reverb state is only ever mutated from here (queued requests
        # above), so nothing below needs a lock either.
        if preset is not None:
            self.reverb.set_preset(preset)
            self._reverb_idle_limit = self._idle_limit()
        if do_reset:
            self._reset_when_quiet = True
        if self._reset_when_quiet and not self._voices and not mixed:
            # Deferred until a block that mixed nothing at all, so that a Panic
            # which is fading its voices out over `DECLICK_TAU` clears the tail
            # that fade itself put into the tank, rather than zeroing the lines in
            # front of it and letting the last 10 ms ring on for the whole of the
            # preset's decay.
            self.reverb.reset()
            # Hard, not faded: this branch only runs on a block that mixed
            # nothing at all, so there is no signal for a zero to step from --
            # and a mute has to leave the bank with nothing to resurrect.
            self.resonance.reset()
            self._reset_when_quiet = False
        if silent:
            # Nothing to filter, no gain to apply: the block is already zeros.
            # The stream stays open, so unmuting is one flag away - no device to
            # re-acquire, nothing to fail.
            return out
        # Before the room, not after: the sympathetic strings are part of the
        # instrument, so what they add has to reach the walls like everything
        # else the instrument does.
        self.resonance.process(out)
        if self._idle_frames < self._reverb_idle_limit:
            self.reverb.process(out)
            self._reverb_settled = False
        elif self.reverb.enabled and not self._reverb_settled:
            # First block past the tail bound. Whatever is left in the tank is
            # under TAIL_FLOOR, so zeroing it is exact to well past the last bit
            # int16 can carry - and it makes every later idle block a single
            # integer compare instead of 24 delay lines. Allocation-free, and
            # `_idle_frames` is back to 0 the moment a voice exists again.
            self.reverb.reset()
            self._reverb_settled = True
        # Static soft-clip: applying the curve unconditionally keeps the
        # transfer function time-invariant, so nothing jumps at block edges.
        # The drive is folded into the volume gain to avoid a temporary array.
        out *= np.float32(self.volume * 1.1)
        np.tanh(out, out=out)
        out *= np.float32(0.92)
        if env is not None:
            # The mute fade is the *last* thing applied, after the clipper, so
            # the gain the ramp asks for is the gain that reaches the device.
            out *= env[:, None]
        return out

    def _callback(self, outdata, frames, time_info, status):  # pragma: no cover
        try:
            outdata[:] = self.render(frames)
        except Exception:
            outdata.fill(0.0)


# ---------------------------------------------------------------- device list
def list_output_devices():
    """Return [(index, name)] of available audio output devices."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception:
        return []
    result = []
    for i, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) >= 2:
            result.append((i, dev.get("name", f"Device {i}")))
    return result
