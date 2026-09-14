"""
The sampled instruments: real recordings instead of a model.

`tone.py` is a good model and it is still what the Rhodes and the Wurlitzer are
made of -- an electric piano is a struck bar behind a pickup, which is a small
enough system to model honestly. An acoustic piano is not, and the gap is not in
the notes. Measured over a six-note chord, the synthesised grand came out at
0.994 L/R correlation with its side channel 25 dB down: a mono signal. The same
measurement on these recordings gives correlation between +0.3 and -0.35 and a
side channel within 3 dB of the middle. That is 25 dB of stereo image no
partial-series work was ever going to supply.

**Two packs, two instruments, both redistributable.**

  * `assets/samples/salamander` -- Salamander Grand Piano V3 by Alexander Holm,
    CC-BY 3.0. A Yamaha C5 under two AKG C414s. Serves `grand` and `felt`.
  * `assets/samples/uprightkw` -- Upright Piano KW by Gonzalo and Roberto
    (FreePats), CC0. A Kawai upright recorded in a living room with a Zoom H1
    where the player's head would be. Serves `upright`.

The upright is a *recording of an upright*, not the grand with the treble taken
off. That distinction is worth the 6.5 MB: an upright's strings are short enough
that its inharmonicity is several times a grand's, and no equaliser adds
inharmonicity. `felt` is the one derived voicing here, and it is derived
honestly -- a moderator strip is a piece of felt lowered between the hammers and
the strings of *the same instrument*, so the grand's own recordings are exactly
the right source for it.

**What a pack is.** A directory of FLAC plus a `pack.json` that says which
recording covers which keys at which velocity, so adding an instrument is
dropping in a folder rather than editing this file. Nothing here knows the name
of a sample.

**Why so little ships.** 19 MB for the grand and 6.5 MB for the upright, because
only the recorded pitches ship: every other key reads a neighbour at a different
rate, at most a semitone away for the grand. Measured against an exact
windowed-sinc resample of real piano samples, a semitone of the mixer's
Catmull-Rom read lands at 75-85 dB SNR -- past what 16-bit source material can
carry.

**Why there is a cache.** Decoding a pack takes about half a second, so the
cache is not there for speed. It is there so the samples are a file-backed
mapping rather than ~90 MB of heap, exactly as `samplebank` keeps the
synthesised banks: an app that idles in the menu bar all day should be holding
pages the OS is free to take back.
"""

from __future__ import annotations

import json
import math
import mmap
import os
import threading
from typing import Callable, Optional

import numpy as np

from . import tone
from .config import CACHE_DIR
from .samplebank import to_pcm16

#: Bumped whenever a pack or the way it is decoded changes, so a stale blob is
#: rebuilt rather than served.
PACK_VERSION = 2

SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "samples")

#: Output trim, so switching between a sampled instrument and a modelled one is
#: not a jump in volume. Measured, not chosen: this puts a mid-velocity chord
#: within half a dB of what the modelled `grand` produced at the same velocity.
LEVEL = 1.25


def _eq(points):
    """A magnitude response as (Hz, dB) breakpoints, interpolated in log-f."""
    hz = np.array([p[0] for p in points], dtype=np.float64)
    db = np.array([p[1] for p in points], dtype=np.float64)
    return np.log10(hz), db


#: A moderator strip -- a felt piano -- is not "a bit darker". The felt sits
#: between hammer and string and eats the strike, so what is left is nearly all
#: fundamental: quiet, round, and gone sooner because the felt is still touching
#: the string as it rings. This is that, measured as a response rather than
#: guessed at: -14 dB by 1 kHz and -40 by 4, which takes a chord's spectral
#: centroid from about 880 Hz to about 420 and its level down about 6 dB.
_FELT_EQ = _eq([(20, 0.0), (150, 0.0), (350, -2.0), (700, -7.0), (1000, -14.0),
                (2000, -26.0), (4000, -40.0), (8000, -52.0), (22050, -60.0)])

#: The instruments, and what each does to its pack.
#:   pack       : the directory under assets/samples
#:   eq         : magnitude response applied at decode time, or None
#:   gain       : output trim
#:   vel_scale  : velocity remap before the layers are chosen. Below 1.0 a hard
#:                blow reaches a softer recording, which is what a moderator does
#:                mechanically -- it does not just turn the piano down.
#:   trim_s     : fraction of each recording's length to keep
#:   noise      : how loud this instrument's own key and pedal noise sits
VOICINGS = {
    "grand":   dict(pack="salamander", eq=None, gain=1.0,
                    vel_scale=1.0, trim_s=1.0, noise=1.0),
    "felt":    dict(pack="salamander", eq=_FELT_EQ, gain=0.88,
                    vel_scale=0.55, trim_s=0.68, noise=0.8),
    # A real upright, so it is left alone. It was recorded in a living room off a
    # single portable recorder and it already sounds like what it is.
    "upright": dict(pack="uprightkw", eq=None, gain=0.85,
                    vel_scale=1.0, trim_s=1.0, noise=1.35),
}

DEFAULT_VOICING = "grand"


def is_sampled(voicing: str) -> bool:
    """Whether `voicing` comes from recordings rather than from `tone`."""
    return voicing in VOICINGS


def pack_dir(voicing: str) -> str:
    return os.path.join(SAMPLES_DIR, VOICINGS[voicing]["pack"])


def pack_present(voicing: str) -> bool:
    """Whether this instrument's recordings are actually installed.

    A source checkout may not have them and a bundle may have been built before
    they existed; `make_bank` falls back to the model rather than failing to
    start, so this has to be answerable without raising.
    """
    return voicing in VOICINGS and os.path.exists(
        os.path.join(pack_dir(voicing), "pack.json"))


def load_manifest(voicing: str) -> dict:
    with open(os.path.join(pack_dir(voicing), "pack.json"), encoding="utf-8") as fh:
        return json.load(fh)


def credits() -> list[str]:
    """One line per installed pack, for the About box and the README.

    CC-BY asks for attribution and CC0 does not, but both are credited: the
    licence is the floor, not the intent.
    """
    out, seen = [], set()
    for voicing in VOICINGS:
        pack = VOICINGS[voicing]["pack"]
        if pack in seen or not pack_present(voicing):
            continue
        seen.add(pack)
        try:
            meta = load_manifest(voicing)
        except (OSError, ValueError):
            continue
        out.append("%s\n%s" % (meta.get("credit", pack), meta.get("url", "")))
    return out


def make_bank(voicing: str, samplerate: int = tone.SAMPLE_RATE):
    """The bank that serves `voicing`: recordings where there are any.

    Every reason this can fall back to the model is a real one -- a checkout
    without the packs, a bundle built before them, a broken `soundfile` wheel.
    `tone` still renders every voicing, so the fallback is always a piano.
    """
    from .samplebank import SampleBank
    if is_sampled(voicing) and pack_present(voicing):
        try:
            import soundfile  # noqa: F401
        except Exception:
            pass
        else:
            return SampledBank(voicing, samplerate)
    return SampleBank(voicing, samplerate)


class SampledBank:
    """Real recordings, decoded once and then memory-mapped.

    Interface-compatible with `samplebank.SampleBank` wherever `synth` touches
    it. What differs is answered by asking -- `stereo` is True here and False
    there, `layer_velocities` is as long as the pack says -- rather than by the
    mixer knowing which kind of bank it holds.
    """

    stereo = True

    def __init__(self, voicing: str = DEFAULT_VOICING,
                 samplerate: int = tone.SAMPLE_RATE) -> None:
        self.voicing = voicing if voicing in VOICINGS else DEFAULT_VOICING
        self.samplerate = samplerate
        self.meta = load_manifest(self.voicing)
        self.layer_velocities = tuple(self.meta["layer_velocities"])
        self.noise_scale = float(VOICINGS[self.voicing]["noise"])
        #: (note, layer) -> (file, playback rate). Built from the manifest, so
        #: nothing here has to know how a pack lays its keys out.
        self._map: dict[tuple[int, int], tuple[str, float]] = {}
        for region in self.meta["regions"]:
            for note in range(int(region["lo"]), int(region["hi"]) + 1):
                step = 2.0 ** ((note - int(region["kc"])) / 12.0)
                self._map[(note, int(region["layer"]))] = (region["file"], step)
        self._notes: dict[str, np.ndarray] = {}
        self._rel: dict[int, np.ndarray] = {}
        self._pedal: dict[str, np.ndarray] = {}
        self._blob: Optional[np.ndarray] = None
        self._map_obj: Optional[mmap.mmap] = None
        self._ranges: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self.ready = False
        self.progress = 0.0
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ paths
    @property
    def _stem(self) -> str:
        return f"sampled_v{PACK_VERSION}_{self.voicing}_{self.samplerate}"

    @property
    def _blob_path(self) -> str:
        return os.path.join(CACHE_DIR, f"{self._stem}.npy")

    @property
    def _index_path(self) -> str:
        return os.path.join(CACHE_DIR, f"{self._stem}.json")

    @property
    def mapped(self) -> bool:
        return self._blob is not None

    # ----------------------------------------------------------------- access
    def layer_mix(self, velocity: float):
        """`(lo, hi, blend, gain)` for a MIDI velocity.

        The recordings carry their own loudness -- a soft layer *is* quieter, not
        just duller -- so unlike the modelled banks there is no velocity curve
        applied on top of the crossfade, which would count the dynamics twice.
        The only gain here is below the softest recording, where there is nothing
        quieter to fade towards and the level has to come from somewhere.
        """
        vels = self.layer_velocities
        if len(vels) == 1 or velocity <= vels[0]:
            # Under the softest recording there is nothing quieter to fade
            # towards, so the level has to come from a gain either way; whichever
            # of the two laws is steeper there is the one that applies.
            below = max(velocity / max(vels[0], 1e-9), 0.0) ** 0.7
            return 0, 0, 0.0, min(below, self._dynamic_gain(velocity))
        if velocity >= vels[-1]:
            return len(vels) - 1, len(vels) - 1, 0.0, 1.0
        hi = 1
        while hi < len(vels) - 1 and velocity > vels[hi]:
            hi += 1
        lo = hi - 1
        span = vels[hi] - vels[lo]
        if span <= 0:
            return lo, hi, 0.0, 1.0
        # `blend` is how much of the gap between two recordings the crossfade
        # occupies, and it is a property of the pack, not of this code. A library
        # sampled at sixteen even steps means each recording to stand for a
        # *point* on the velocity scale, so the fade runs the whole way and 1.0
        # is right. One sampled as two ranges -- soft up to 80, loud from 81 --
        # means each recording to stand for its whole range, and blending it
        # across all of that reaches the loud recording far too early: measured
        # on the upright, the full fade put it 5 dB over the grand on an ordinary
        # phrase while a single mid-velocity chord looked level.
        frac = (velocity - vels[lo]) / span
        blend = float(self.meta.get("layer_blend", 1.0))
        if blend < 1.0:
            edge = 0.5 - blend * 0.5
            frac = min(max((frac - edge) / max(blend, 1e-9), 0.0), 1.0)
        return lo, hi, frac, self._dynamic_gain(velocity)

    def _dynamic_gain(self, velocity: float) -> float:
        """The loudness the recordings do not supply on their own.

        A library sampled at sixteen velocities carries a piano's whole dynamic
        range in the recordings themselves, and anything added here would be
        counting it twice -- so `dynamic_db` is 0 and this is 1.0. A library
        sampled at two carries the *timbre* of soft and loud playing but only the
        span between two microphone levels: measured against the grand, the
        upright came out 10 dB too loud at velocity 30 and 2 dB too quiet at 127,
        which is not a trim, it is a missing dynamic range. `dynamic_db` is how
        much of one to put back, as a tilt that reaches 0 at full velocity.
        """
        span_db = float(self.meta.get("dynamic_db", 0.0))
        if span_db <= 0.0:
            return 1.0
        v = min(max(velocity, 0.0), 127.0) / 127.0
        return float(10.0 ** (span_db * (v - 1.0) / 20.0))

    def note_mix(self, note: int, velocity: float, curve: float):
        """`(lo, hi, w_lo, w_hi, step, amp)` -- see `SampleBank.note_mix`.

        The velocity curve is applied as a *remap* of the velocity, before the
        recordings are chosen, rather than as a gain afterwards. That is what a
        touch curve does on a sampler: it decides which recording a given blow
        reaches, not how loud that recording is played.
        """
        note = min(max(int(note), tone.NOTE_MIN), tone.NOTE_MAX)
        v = 127.0 * (min(max(velocity, 0.0), 127.0) / 127.0) ** curve
        v *= float(VOICINGS[self.voicing]["vel_scale"])
        lo, hi, blend, gain = self.layer_mix(v)
        got_lo = self._map.get((note, lo))
        got_hi = self._map.get((note, hi))
        if got_lo is None or got_hi is None:
            return None
        buf_lo = self._notes.get(got_lo[0])
        buf_hi = self._notes.get(got_hi[0])
        if buf_lo is None or buf_hi is None:
            return None
        # Both layers are read at the louder one's rate. They come from regions
        # that may centre on different keys, and two rates in one voice would be
        # two voices; the blend is short-lived and the difference never exceeds
        # what the crossfade is already smoothing over.
        step = got_hi[1] if blend >= 0.5 else got_lo[1]
        return buf_lo, buf_hi, 1.0 - blend, blend, step, gain * LEVEL

    def release(self, note: int) -> Optional[np.ndarray]:
        """The damper landing on this string, and the key coming back."""
        return self._rel.get(min(max(int(note), tone.NOTE_MIN), tone.NOTE_MAX))

    def pedal_noise(self, down: bool, alt: bool = False) -> Optional[np.ndarray]:
        return self._pedal.get(("D" if down else "U") + ("2" if alt else "1"))

    def prefault(self, note: int) -> None:
        """Make one key's recordings resident, off the audio thread.

        Same reason as `samplebank.SampleBank.prefault`: the mapping exists so
        the OS *may* reclaim these pages, which means the mixer would otherwise
        pay the fault inside the callback.
        """
        mp = self._map_obj
        if mp is None:
            return
        for layer in range(len(self.layer_velocities)):
            got = self._map.get((note, layer))
            if got is None:
                continue
            rng = self._ranges.get(got[0])
            if rng is None:
                continue
            try:
                mp.madvise(mmap.MADV_WILLNEED, rng[0], max(rng[1] - rng[0], 1))
            except (AttributeError, OSError, ValueError):
                return

    # ------------------------------------------------------------------ build
    def load_or_build_async(self, on_progress: Optional[Callable[[float], None]] = None,
                            on_done: Optional[Callable[[], None]] = None) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._load_or_build, args=(on_progress, on_done), daemon=True)
        self._thread.start()

    def build_blocking(self, on_progress=None) -> None:
        self._load_or_build(on_progress, None)

    def _load_or_build(self, on_progress, on_done) -> None:
        try:
            if not self._load_cache():
                self._decode(on_progress)
                self._save_cache()
                self._load_cache()          # swap the heap copy for the mapping
            self.ready = True
            self.progress = 1.0
        except Exception as exc:            # pragma: no cover - surfaced in the UI
            self.error = str(exc)
        finally:
            if on_done:
                on_done()

    def _shape(self, a: np.ndarray, v: dict, trim: bool) -> np.ndarray:
        a = np.asarray(a, dtype=np.float32)
        if a.ndim == 1:
            a = np.stack((a, a), axis=1)
        elif a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        if v["eq"] is not None and len(a):
            # Zero-phase, applied once offline: a piano note is not a signal a
            # minimum-phase filter's group delay flatters, and this costs
            # nothing at play time.
            n = 1 << max(1, int(len(a) - 1)).bit_length()
            spec = np.fft.rfft(a, n=n, axis=0)
            freq = np.fft.rfftfreq(n, 1.0 / self.samplerate)
            log_hz, db = v["eq"]
            gain = np.interp(np.log10(np.maximum(freq, 1.0)), log_hz, db)
            spec *= (10.0 ** (gain / 20.0))[:, None]
            a = np.fft.irfft(spec, n=n, axis=0)[:len(a)].astype(np.float32)
        if trim and v["trim_s"] < 1.0:
            n = max(int(len(a) * float(v["trim_s"])), 1)
            a = a[:n].copy()
            fade = min(int(0.12 * self.samplerate), n // 4)
            if fade > 0:
                a[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
        return a * np.float32(v["gain"])

    def _decode(self, on_progress) -> None:
        import soundfile as sf

        v = VOICINGS[self.voicing]
        base = pack_dir(self.voicing)
        files = sorted({r["file"] for r in self.meta["regions"]})
        rel_prefix = self.meta.get("release_prefix")
        pedal_prefix = self.meta.get("pedal_prefix")
        extra = (tone.NOTE_MAX - tone.NOTE_MIN + 1 if rel_prefix else 0) + \
                (4 if pedal_prefix else 0)
        total = len(files) + extra
        done = 0

        for name in files:
            a, _ = sf.read(os.path.join(base, name), dtype="float32", always_2d=True)
            self._notes[name] = to_pcm16(self._shape(a, v, True))
            done += 1
            self.progress = done / total
            if on_progress:
                on_progress(self.progress)

        if rel_prefix:
            for note in range(tone.NOTE_MIN, tone.NOTE_MAX + 1):
                path = os.path.join(base, "%s%03d.flac" % (rel_prefix, note))
                if os.path.exists(path):
                    a, _ = sf.read(path, dtype="float32", always_2d=True)
                    self._rel[note] = to_pcm16(self._shape(a, v, False))
                done += 1
                self.progress = done / total
        if pedal_prefix:
            for key in ("D1", "D2", "U1", "U2"):
                path = os.path.join(base, "%s%s.flac" % (pedal_prefix, key))
                if os.path.exists(path):
                    a, _ = sf.read(path, dtype="float32", always_2d=True)
                    self._pedal[key] = to_pcm16(self._shape(a, v, False))
                done += 1
        self.progress = 1.0

    # ------------------------------------------------------------------ cache
    def _entries(self):
        for name, buf in sorted(self._notes.items()):
            yield "n:" + name, buf
        for note, buf in sorted(self._rel.items()):
            yield "r:%d" % note, buf
        for key, buf in sorted(self._pedal.items()):
            yield "p:" + key, buf

    def _save_cache(self) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        index, offset = {}, 0
        for key, buf in self._entries():
            index[key] = [offset, offset + buf.size, buf.shape[1]]
            offset += buf.size
        flat = np.empty(offset, dtype=np.int16)
        for key, buf in self._entries():
            start, stop, _ = index[key]
            flat[start:stop] = buf.reshape(-1)
        tmp_blob, tmp_index = self._blob_path + ".tmp", self._index_path + ".tmp"
        # Written through a handle, not a name: `np.save` appends ".npy" to a
        # path that does not already end in it, so saving to "<stem>.npy.tmp"
        # silently produces "<stem>.npy.tmp.npy" and the rename finds nothing.
        with open(tmp_blob, "wb") as fh:
            np.save(fh, flat, allow_pickle=False)
        with open(tmp_index, "w", encoding="utf-8") as fh:
            json.dump({"version": PACK_VERSION, "voicing": self.voicing,
                       "samplerate": self.samplerate, "index": index}, fh)
        os.replace(tmp_blob, self._blob_path)
        os.replace(tmp_index, self._index_path)

    def _load_cache(self) -> bool:
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return False
        if (meta.get("version") != PACK_VERSION
                or meta.get("voicing") != self.voicing
                or meta.get("samplerate") != self.samplerate):
            return False
        try:
            blob = np.load(self._blob_path, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError):
            return False
        notes, rel, pedal, ranges = {}, {}, {}, {}
        header = int(getattr(blob, "offset", 0) or 0)
        for key, (start, stop, channels) in meta["index"].items():
            view = np.asarray(blob[start:stop]).reshape(-1, channels)
            kind, name = key.split(":", 1)
            if kind == "n":
                notes[name] = view
                ranges[name] = (header + start * 2, header + stop * 2)
            elif kind == "r":
                rel[int(name)] = view
            else:
                pedal[name] = view
        self._notes, self._rel, self._pedal = notes, rel, pedal
        self._blob = blob
        self._ranges = ranges
        self._map_obj = getattr(getattr(blob, "base", None), "_mmap", None) \
            or getattr(blob, "_mmap", None)
        return True
