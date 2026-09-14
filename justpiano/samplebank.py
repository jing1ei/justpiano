"""
Piano sample bank: renders every key x velocity-layer once, keeps them in RAM
as int16, and caches the result on disk so subsequent launches are instant.

Rendering is done on a background thread pool. Notes around the middle of the
keyboard are rendered first, so you can start playing almost immediately while
the extremes are still being built.

Each bank belongs to exactly one voicing (`tone.VOICINGS`) and caches under its
own blob/index pair, so switching to a voicing that has been built before -- and
switching back -- costs a file read rather than a full re-render.

The cached blob is *memory-mapped*, not read: 44.6 MiB of samples that are never
written to are file-backed clean pages the OS can drop and fault back in, rather
than dirty anonymous heap it has to keep (or swap). A session that only plays two
octaves therefore only ever pays for the keys it touches.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import mmap
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

import numpy as np

from . import tone
from .config import CACHE_DIR

#: 4: `to_pcm16` rounds instead of truncating, so every cached sample can differ
#: from a v3 blob by one LSB. The bytes changed without `tone` changing, and
#: `TONE_FINGERPRINT` only covers `tone` -- without this bump every existing
#: installation would keep serving its subtly-wrong cache for ever.
BANK_VERSION = 4

#: Shared prefix of every cache stem this module has ever written. `prune()`
#: uses it to recognise its own litter and nothing else in `CACHE_DIR`.
CACHE_PREFIX = "bank_v"

#: `_save_cache()` publishes a stem in two steps (blob, then index) and writes
#: through `.tmp` siblings, so for a few hundred milliseconds the files on disk
#: are indistinguishable from the orphans `prune_cache()` exists to delete. Two
#: banks overlap whenever the user picks another voicing before the first build
#: has finished (`tray._swap_bank` starts a new `SampleBank` while the old build
#: thread is still alive), so this is a normal sequence, not a pathological one:
#:
#:   * `_CACHE_LOCK` serialises writers against pruners for the whole write, and
#:   * `_WRITING` names the stems a writer is part-way through, so a prune that
#:     started first (and is therefore holding the lock) still leaves them alone.
#:
#: Stale `.tmp` files from a *crashed* writer are not in `_WRITING` and are still
#: pruned, which is the whole point of pruning them.
_CACHE_LOCK = threading.RLock()
_WRITING: set[str] = set()

#: Build thread pool size. Measured on a 64-core host: 1 worker 2.67 s, 2 1.68,
#: **3 1.43**, 4 1.56, 8 1.98, 12 2.11. `tone.render_note` is numpy-bound but
#: spends real time in Python between the vector ops, so past three threads the
#: GIL turns extra workers into contention: the eight this used to ask for were
#: 33 % slower in wall clock, burned 6.6 s of CPU instead of 3.4, and lifted the
#: build's peak RSS to 139 MiB from 103 (that many buffers in flight at once).
MAX_BUILD_WORKERS = 3

#: Stride, in samples, of the read `prefault()` uses to fault a buffer in: one
#: int16 per 4 KiB page (`mmap.PAGESIZE`), which is the coarsest read that still
#: touches every page of the mapping.
_PREFAULT_STRIDE = max(1, mmap.PAGESIZE // 2)

#: How many instruments may keep a bank on disk at once. Each one is 32-48 MiB,
#: so the five this build ships come to ~198 MiB if every one of them is ever
#: chosen -- which nothing would ever reclaim, because a cache that still matches
#: this build is by definition one `prune_cache()` must not touch. Three is the
#: number that keeps "switch away and back" a file read (the instrument playing,
#: plus the two most recently played before it) without letting a menu the user
#: browsed once cost a fifth of a gigabyte for ever. The bank in use is always
#: kept, whatever its position: `prune_cache(keep=...)` puts it first.
MAX_CACHED_BANKS = 3


def to_pcm16(buf: np.ndarray, dtype=np.int16) -> np.ndarray:
    """Quantise a float buffer in [-1, 1] to 16-bit PCM, clipping the overshoot.

    The one place the sample format is decided, shared by the bank and the WAV
    exporter (`recorder.export_wav` passes the little-endian `"<i2"` a RIFF file
    wants). +32767 rather than +32768 so a full-scale peak cannot wrap.

    Rounded, not truncated: `astype` cuts towards zero, so it lost up to a whole
    LSB per sample (0.99999 came out as 32766, and everything under half an LSB
    came out as digital silence) and biased the entire bank towards zero -- a
    quantisation error twice as large as the format has to carry, on every note.
    """
    scaled = np.rint(buf * 32767.0)
    return np.clip(scaled, -32768, 32767, out=scaled).astype(dtype)


def _code_signature(code) -> tuple:
    """Version-stable digest material for one function's code object.

    `repr()` of a *nested* code object (a comprehension, say) embeds its memory
    address, so constants are flattened recursively -- otherwise the fingerprint
    would differ on every launch and rebuild the whole bank each time.
    """
    consts = tuple(_code_signature(c) if hasattr(c, "co_code") else repr(c)
                   for c in code.co_consts)
    return (code.co_code, consts, code.co_names)


def _tone_fingerprint() -> str:
    """Digest of the tone model, so a changed synth invalidates the cache.

    Every knob that shapes a sample (`_LAYER_PARAMS`, `render_note`,
    `note_duration`, `inharmonicity`, ...) lives in `tone`, and none of them are
    covered by `BANK_VERSION` unless a human remembers to bump it.

    The shipped .app is a PyInstaller bundle with `noarchive=False` and no
    `justpiano/*.py` in `datas`, so `inspect.getsource(tone)` *always* fails
    there: for real users the source hash is not the mechanism, it is a bonus.
    What has to carry the load in a frozen build is therefore folded in first:

      * `tone.TONE_MODEL_REVISION` -- hand-bumped generation marker,
      * the module's constants, and
      * the bytecode + constants of its functions (these survive into the PYZ).

    The source is appended only when it happens to be readable, as an extra
    signal that also catches an edit whose revision bump was forgotten.
    """
    parts = [f"revision={tone.TONE_MODEL_REVISION}"]
    for name in sorted(vars(tone)):
        if name.startswith("__"):
            continue
        value = getattr(tone, name)
        code = getattr(value, "__code__", None)
        if code is not None:
            parts.append(f"{name}={_code_signature(code)!r}")
        elif isinstance(value, (bool, int, float, str, bytes, tuple, list, dict)):
            parts.append(f"{name}={value!r}")
        elif isinstance(value, np.ndarray):
            parts.append(f"{name}={value.dtype}{value.shape}{value.tobytes()!r}")
    try:
        parts.append(inspect.getsource(tone))
    except Exception:
        pass  # normal in the shipped bundle; the signals above stand alone
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


TONE_FINGERPRINT = _tone_fingerprint()


def _bank_fingerprint(voicing: str) -> str:
    """Cache identity of one voicing's bank: the tone digest with the voicing id
    folded in.

    `TONE_FINGERPRINT` already covers the parameters of *every* voicing (they are
    module-level constants of `tone`), but not which one a given blob was
    rendered with -- that is what the id adds here, and it is checked on load in
    addition to the file name.
    """
    key = f"{TONE_FINGERPRINT}\x1fvoicing={voicing}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class SampleBank:
    """Thread-safe store of rendered piano notes."""

    def __init__(self, voicing: str = tone.DEFAULT_VOICING,
                 samplerate: int = tone.SAMPLE_RATE) -> None:
        # An unknown id would render as the default (see `tone._voicing`) but
        # cache under its own name, so it is normalised here instead.
        self.voicing = voicing if voicing in tone.VOICINGS else tone.DEFAULT_VOICING
        self.fingerprint = _bank_fingerprint(self.voicing)
        self.samplerate = samplerate
        self._data: dict[tuple[int, str], np.ndarray] = {}
        #: The mapped `.npy` behind `_data` once it came from the cache. Held so
        #: the mapping outlives the views into it (they keep it alive through
        #: `.base` too, but a build that never loaded has to read as None).
        self._blob: Optional[np.ndarray] = None
        #: The `mmap.mmap` under `_blob`, and the byte range of every buffer
        #: inside it: `prefault()` needs both to advise the kernel about the
        #: pages one key is about to be read from.
        self._map: Optional[mmap.mmap] = None
        self._map_ranges: dict[tuple[int, str], tuple[int, int]] = {}
        self._lock = threading.Lock()
        self.ready = False
        self.progress = 0.0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ paths
    @property
    def _stem(self) -> str:
        return f"{CACHE_PREFIX}{BANK_VERSION}_{self.voicing}_{self.samplerate}"

    @property
    def _blob_path(self) -> str:
        return os.path.join(CACHE_DIR, f"{self._stem}.npy")

    @property
    def _index_path(self) -> str:
        return os.path.join(CACHE_DIR, f"{self._stem}.json")

    # ------------------------------------------------------------------ access
    def get(self, note: int, layer: str) -> Optional[np.ndarray]:
        return self._data.get((note, layer))

    #: False: `tone` renders one channel and `synth` pans it. The sampled bank
    #: answers True, and the mixer asks rather than knowing which is which.
    stereo = False

    #: MIDI velocities the two rendered layers stand for. The mixer crossfades
    #: between whichever pair brackets the velocity played, which for two layers
    #: is the (velocity - 12) / 100 ramp this has always used.
    layer_velocities = (12.0, 112.0)

    def note_mix(self, note: int, velocity: float, curve: float):
        """Everything the mixer needs to sound one note, or None if not ready.

        `(lo, hi, w_lo, w_hi, step, amp)`. Each bank owns its own dynamics: here
        the layers are two fixed timbres and the loudness comes from the velocity
        curve, whereas a bank of recordings gets its loudness from which
        recording is playing. Keeping that behind one call is what lets `synth`
        hold both without a branch.
        """
        pair = self.get_pair(note)
        if pair is None:
            return None
        soft, hard = pair
        w_hi = min(1.0, max(0.0, (velocity - 12.0) / 100.0))
        amp = ((velocity / 127.0) ** curve
               * tone.note_gain(note, self.voicing) * 0.55)
        return soft, hard, 1.0 - w_hi, w_hi, 1.0, amp

    def release(self, note: int):
        """No recorded key-off: the model's release is an envelope, not a sound."""
        return None

    def pedal_noise(self, down: bool, alt: bool = False):
        return None

    def get_pair(self, note: int):
        """Return (soft, hard) buffers for a note, or None if not rendered yet."""
        soft = self._data.get((note, "soft"))
        hard = self._data.get((note, "hard"))
        if soft is None or hard is None:
            return None
        return soft, hard

    @property
    def mapped(self) -> bool:
        """True while the samples are file-backed pages rather than heap.

        The whole point of the mmap: a mapped bank is clean memory the OS may
        reclaim under pressure and fault back in, so an app that idles in the
        menu bar all day is not holding 44.6 MiB hostage.
        """
        return self._blob is not None

    def prefault(self, note: int) -> None:
        """Make one key's two buffers resident, off the audio thread.

        The mmap exists so the OS *can* reclaim these pages, and the mixer is the
        only reader -- so the first block of a note played after an idle period
        used to pay the fault inside the callback: measured at 7.9 ms with 48
        major faults for a 5.8 ms budget, i.e. a dropout. `AudioEngine.note_on`
        calls this from the MIDI thread, where a page fault costs a note a
        fraction of a millisecond of latency instead of costing the stream a
        block.

        `MADV_WILLNEED` starts the readahead; the strided read then makes
        residency a fact rather than a hint. One int16 per page, through the
        read-only view, so nothing here can dirty a page (which would defeat the
        mapping) and nothing allocates a buffer.
        """
        pair = self.get_pair(note)
        if pair is None:
            return          # not rendered yet, or an unknown key
        mapping = self._map
        if mapping is not None:
            for layer in tone.LAYERS:
                span = self._map_ranges.get((note, layer))
                if span is None:
                    continue
                # madvise wants a page-aligned start; grow the length to match.
                start = span[0] - span[0] % mmap.PAGESIZE
                try:
                    mapping.madvise(mmap.MADV_WILLNEED, start,
                                    span[0] + span[1] - start)
                except (OSError, ValueError, AttributeError):
                    pass    # advice is advice; the read below is what counts
        for buf in pair:
            int(buf[::_PREFAULT_STRIDE].sum(dtype=np.int64))

    # ------------------------------------------------------------------ build
    def load_or_build_async(self, on_progress: Optional[Callable[[float], None]] = None,
                            on_done: Optional[Callable[[], None]] = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._load_or_build, args=(on_progress, on_done),
            name="bank-build", daemon=True,
        )
        self._thread.start()

    def _load_or_build(self, on_progress, on_done) -> None:
        # Only the cache read is allowed to fail silently: a raising callback
        # must not be mistaken for a corrupt cache and trigger a full rebuild.
        try:
            loaded = self._load_cache()
        except Exception:
            loaded = False  # corrupt / stale cache -> just rebuild
        if loaded:
            self.progress = 1.0
            self.ready = True
            self._prune_cache()
            if on_progress:
                on_progress(1.0)
            if on_done:
                on_done()
            return

        try:
            self._build(on_progress)
        except Exception as exc:
            # Without this the thread would die silently and the status line
            # would sit at "Building piano… N%" for the rest of the session.
            self.error = str(exc)
        else:
            self.ready = True
            if self._try_save_cache():
                self._adopt_cache()
            self._prune_cache()
        if on_done:
            on_done()

    def _build(self, on_progress) -> None:
        notes = list(range(tone.NOTE_MIN, tone.NOTE_MAX + 1))
        # Middle of the keyboard first so the app is playable ASAP.
        notes.sort(key=lambda n: abs(n - 64))
        jobs = [(n, layer) for n in notes for layer in tone.LAYERS]
        total = len(jobs)
        done = 0
        done_lock = threading.Lock()

        def work(job):
            nonlocal done
            note, layer = job
            pcm = to_pcm16(self._render(note, layer))
            with self._lock:
                self._data[(note, layer)] = pcm
            with done_lock:
                done += 1
                self.progress = done / total
                if on_progress:
                    on_progress(self.progress)

        workers = max(2, min(MAX_BUILD_WORKERS, (os.cpu_count() or 4)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, jobs))

    def _render(self, note: int, layer: str) -> np.ndarray:
        """Render one buffer for this bank's voicing."""
        return tone.render_note(note, layer, self.voicing, self.samplerate)

    # ------------------------------------------------------------------ cache
    def _save_cache(self) -> None:
        """Write the blob and its index, streaming rather than concatenating.

        The old shape built one 44.6 MiB array out of the 176 buffers just to
        hand it to `np.save`, which doubled the bank's footprint at the worst
        possible moment -- the tail of a first launch, when the render peak has
        not been released yet (130.5 MiB peak against 85.9 steady). The header
        comes from numpy's own writer, so the file stays a plain `.npy` that
        `_load_cache` can memory-map; only the payload is streamed.

        Held under `_CACHE_LOCK` and announced in `_WRITING` from end to end: a
        concurrent bank's `prune_cache()` used to see the two-step publication
        as an orphan and delete it out from under us (see `_CACHE_LOCK`).
        """
        with _CACHE_LOCK:
            _WRITING.add(self._stem)
            try:
                self._write_cache()
            finally:
                _WRITING.discard(self._stem)

    def _write_cache(self) -> None:
        """The actual two-step publication; always called under `_CACHE_LOCK`."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        keys = sorted(self._data.keys())
        offsets = {}
        pos = 0
        for note, layer in keys:
            size = int(self._data[(note, layer)].size)
            offsets[f"{note}:{layer}"] = [pos, size]
            pos += size
        tmp = self._blob_path + ".tmp"
        with open(tmp, "wb") as fh:
            np.lib.format.write_array_header_1_0(fh, {
                "descr": np.lib.format.dtype_to_descr(np.dtype(np.int16)),
                "fortran_order": False,
                "shape": (pos,),
            })
            for note, layer in keys:
                buf = np.ascontiguousarray(self._data[(note, layer)])
                fh.write(buf.data)          # no copy: straight out of the buffer
        os.replace(tmp, self._blob_path)
        tmp = self._index_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"samplerate": self.samplerate, "tone": TONE_FINGERPRINT,
                       "voicing": self.voicing, "fingerprint": self.fingerprint,
                       "offsets": offsets}, fh)
        os.replace(tmp, self._index_path)  # never leave a half-written index

    def _adopt_cache(self) -> None:
        """Swap a freshly built bank over to the mapped copy just written.

        A first launch would otherwise be the one session that pays 44.6 MiB of
        dirty heap for samples that are now sitting on disk anyway. Re-reading
        the file we just wrote also proves it round-trips before the app relies
        on it. Voices already sounding keep the heap buffers they were handed --
        they hold their own reference -- so nothing can be pulled out from under
        the audio thread.
        """
        try:
            self._load_cache()
        except Exception:
            pass   # keep the in-memory build; it is correct, just not mapped

    def _load_cache(self) -> bool:
        if not (os.path.exists(self._blob_path) and os.path.exists(self._index_path)):
            return False
        with open(self._index_path) as fh:
            index = json.load(fh)
        if index.get("samplerate") != self.samplerate:
            return False
        if index.get("tone") != TONE_FINGERPRINT:
            return False  # the tone model changed -> re-render
        # Belt and braces around the per-voicing file name: a blob renamed or
        # written by an older layout must never be read as this voicing.
        if index.get("voicing") != self.voicing:
            return False
        if index.get("fingerprint") != self.fingerprint:
            return False
        # Touching a mapped page that is past the end of its file raises SIGBUS
        # -- a signal, not an exception -- and on the audio thread that is a
        # crash, not a dropout. Two things keep that off the table:
        #
        #  * a blob shorter than its own header never maps at all, because numpy
        #    asks mmap for more bytes than the file holds and mmap refuses, so a
        #    truncated cache is rejected here (rebuild) instead of later (dead);
        #  * nothing ever shortens a *live* blob: `_save_cache` writes a sibling
        #    .tmp and `os.replace`s it, which swaps the directory entry and
        #    leaves this inode intact, and `prune_cache` is never given the stem
        #    in use. An unlinked-but-mapped file stays readable on POSIX.
        try:
            blob = np.load(self._blob_path, mmap_mode="r")
        except (ValueError, OSError, EOFError):
            return False        # truncated, unreadable, or not a .npy at all
        if blob.ndim != 1 or blob.dtype != np.int16:
            return False
        # `np.memmap` slices are ~2x slower to read than plain ndarray slices in
        # the mixer; the view drops the memmap subclass while keeping the same
        # (read-only, file-backed) pages, and `.base` keeps the mapping alive.
        # The mapping itself and the header length are kept for `prefault()`,
        # which needs a byte range inside the mapping to advise on.
        mapping = getattr(blob, "_mmap", None)
        header = int(getattr(blob, "offset", 0))
        itemsize = blob.dtype.itemsize
        blob = blob.view(np.ndarray)
        data = {}
        ranges = {}
        for key, (start, length) in index["offsets"].items():
            note_s, layer = key.split(":")
            note = int(note_s)
            # numpy slicing never raises on out-of-range bounds, so a truncated
            # blob or a stale index would silently yield short buffers.
            if start < 0 or length <= 0 or start + length > blob.size:
                return False
            # Buffer lengths are fixed by `tone.note_duration`, so this catches a
            # changed duration (or a foreign note number) whatever the
            # fingerprint above managed to see.
            if not (tone.NOTE_MIN <= note <= tone.NOTE_MAX) or layer not in tone.LAYERS:
                return False
            if length != int(tone.note_duration(note, self.voicing) * self.samplerate):
                return False
            data[(note, layer)] = blob[start:start + length]
            ranges[(note, layer)] = (header + start * itemsize, length * itemsize)
        expected = (tone.NOTE_MAX - tone.NOTE_MIN + 1) * len(tone.LAYERS)
        if len(data) != expected:
            return False
        with self._lock:
            self._data = data
            self._blob = blob
            self._map = mapping
            self._map_ranges = ranges
        try:
            # `prune_cache` evicts the least recently *used* bank, and a bank
            # that is only ever read would otherwise look as old as the day it
            # was written -- so the instrument you always come back to would be
            # the first one thrown away.
            os.utime(self._index_path, None)
        except OSError:
            pass    # a read-only cache directory is still a usable one
        return True

    def _prune_cache(self) -> None:
        """Best-effort `prune()`; a cache that will not tidy is not an error."""
        try:
            prune_cache(keep=(self._stem,))
        except Exception:
            pass

    def _try_save_cache(self) -> bool:
        """Write the cache; report a failure instead of hiding it.

        The samples are already rendered and playable, so a cache that cannot be
        written is not fatal and `ready` stays true -- but it *is* a real fault:
        it silently costs every future launch a full re-render and, because
        `_adopt_cache` never runs, costs this session 44.6 MiB of dirty heap
        instead of the mmap. `error` is where the bank's faults reach the tray
        (`tray._pick_voicing` reads it too, so re-picking the voicing retries).
        """
        try:
            self._save_cache()
        except Exception as exc:
            self.error = f"sample cache not written: {exc}"
            return False
        return True

    def build_blocking(self, on_progress=None) -> None:
        """Synchronous build (used by CLI tools/tests)."""
        try:
            loaded = self._load_cache()
        except Exception:
            loaded = False  # mirror the async path: bad cache -> rebuild
        if not loaded:
            self._build(on_progress)
            if self._try_save_cache():
                self._adopt_cache()
        self._prune_cache()
        self.ready = True
        self.progress = 1.0


# --------------------------------------------------------------------- pruning
def prune_cache(keep: Iterable[str] = ()) -> list[str]:
    """Delete sample-bank cache files this build can never use again.

    Two rules, because there are two ways for a cache file to outstay its
    welcome:

      * **Unreadable.** Every `BANK_VERSION` or `TONE_FINGERPRINT` bump renames
        every stem, and nothing used to remove the ones left behind: the five
        instruments this build ships are ~198 MiB, orphaned for good, on each
        bump. A stem survives this rule only when its index still matches the
        running build -- which deliberately includes the *other* instruments, so
        switching back to one that was built before stays a file read.
      * **Surplus.** Perfectly readable banks are capped at `MAX_CACHED_BANKS`,
        least recently used evicted first (see `_over_budget`). Without it,
        working through the Instrument menu once leaves every one of them on
        disk for ever, and the first rule -- which is about correctness -- can
        never be the thing that takes them away.

    Runs under `_CACHE_LOCK`, and stems listed in `_WRITING` are kept whatever
    they look like on disk: a `_save_cache` in flight is indistinguishable from
    the orphans this function exists to remove (a `.npy` with no index between
    the two `os.replace`s, or a lone `.tmp` mid-payload), and deleting one cost
    the other voicing its cache -- or made `os.replace` raise. `.tmp` files left
    by a writer that is *not* running are still pruned.

    Returns the paths removed, for the tests and for anyone reading a log.
    """
    with _CACHE_LOCK:
        keep = set(keep) | set(_WRITING)
        try:
            names = os.listdir(CACHE_DIR)
        except OSError:
            return []

        stems: dict[str, list[str]] = {}
        for name in names:
            if not name.startswith(CACHE_PREFIX):
                continue
            # bank_v4_grand_44100.npy / .json / .npy.tmp / .json.tmp
            stem = name.split(".", 1)[0]
            stems.setdefault(stem, []).append(name)

        doomed = [stem for stem in stems
                  if stem not in keep and not _stem_is_current(stem)]
        doomed += _over_budget(
            [stem for stem in stems if stem not in doomed], keep)

        removed = []
        for stem in doomed:
            for name in stems[stem]:
                path = os.path.join(CACHE_DIR, name)
                try:
                    os.remove(path)
                except OSError:
                    continue
                removed.append(path)
        return removed


def _over_budget(stems: Iterable[str], keep: set) -> list[str]:
    """The banks past `MAX_CACHED_BANKS`, least recently used first.

    Called with the stems that survived the "this build can never read it"
    sweep, i.e. the ones that are still perfectly good -- which is exactly why
    they need a second rule: nothing else will ever remove them, and five
    instruments is ~198 MiB of samples for an app that ships no audio at all.

    Recency is the index file's mtime, which `_write_cache` sets when a bank is
    built and `_load_cache` refreshes every time one is read, so this evicts the
    instrument that has gone longest without being played. Anything in `keep`
    (the bank in use, and any that a `_save_cache` is part-way through) is held
    whatever its age and still counts against the budget.
    """
    stems = list(stems)
    if len(stems) <= MAX_CACHED_BANKS:
        return []

    def used_at(stem: str) -> float:
        if stem in keep:
            return float("inf")     # in use: never a candidate
        try:
            return os.path.getmtime(os.path.join(CACHE_DIR, f"{stem}.json"))
        except OSError:
            return 0.0              # no index to date it by: evict it first
    stems.sort(key=used_at, reverse=True)
    return [stem for stem in stems[MAX_CACHED_BANKS:] if stem not in keep]


def _stem_is_current(stem: str) -> bool:
    """Could *this* build still load `stem` from the cache directory?

    Answered from the index file rather than the name, so a blob whose tone
    fingerprint has moved on is pruned even though its file name has not.
    """
    if not stem.startswith(f"{CACHE_PREFIX}{BANK_VERSION}_"):
        return False
    index_path = os.path.join(CACHE_DIR, f"{stem}.json")
    blob_path = os.path.join(CACHE_DIR, f"{stem}.npy")
    if not (os.path.exists(index_path) and os.path.exists(blob_path)):
        return False   # a half of a pair is unusable on its own
    try:
        with open(index_path) as fh:
            index = json.load(fh)
    except Exception:
        return False
    voicing = index.get("voicing")
    return (index.get("tone") == TONE_FINGERPRINT
            and voicing in tone.VOICINGS
            and index.get("fingerprint") == _bank_fingerprint(voicing)
            and stem == f"{CACHE_PREFIX}{BANK_VERSION}_{voicing}_"
                        f"{index.get('samplerate')}")

