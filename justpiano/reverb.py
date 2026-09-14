"""
The room: asymmetric early reflections, then a block-vectorised Freeverb tail.

The tail alone was measured to be the smaller half of this. A Freeverb tank is
*diffuse from the first sample* -- ~6700 echoes a second by 50 ms, and not one
discrete reflection before that -- so it supplies decay without supplying a
room. Nothing in it says how big the space is or where the instrument stands in
it, and a piano needs both: the first thing a listener localises is the pattern
of distinct early reflections, not the fog after them.

It was also, in practice, inaudible as a stereo cue. The tank decorrelates well
(0.02 L/R correlation on its own) but sits ~20 dB under the dry signal, and the
dry signal is a mono buffer amplitude-panned by pitch: measured over a six-note
chord, the whole output came out at 0.994 L/R correlation with its side channel
25 dB down. That is a mono recording. No piano sounds like that, and it is a
much louder "synthetic" cue than anything in the notes themselves.

So `_ER_TAPS_L` / `_ER_TAPS_R` put a real reflection pattern in front of the
tank: two sets of a dozen taps between 8 and 75 ms, at no shared time and with
alternating polarity, which is what a pair of microphones in a room actually
receives. They decorrelate every note -- including a note played on its own,
which no amount of panning can widen -- and the tank inherits them, so the tail
starts from the room rather than from the dry signal.

Every comb/allpass delay line is longer than the processing chunk (128 frames),
which means the recursive feedback only ever reads samples written in *previous*
chunks. That lets the whole thing run as a handful of numpy slice operations
instead of a per-sample Python loop -- fast enough for the audio callback.
"""

from __future__ import annotations

import math

import numpy as np

CHUNK = 128  # must be <= the shortest delay line below

#: Early reflections, as (delay in ms, gain). Two patterns that share no arrival
#: time, so the two channels are decorrelated by geometry rather than by a
#: constant offset -- Freeverb's `_STEREO_SPREAD` trick works on the tank only
#: because the tank is chaotic; a dozen discrete taps offset by half a
#: millisecond would just read as one comb-filtered signal.
#:
#: Times thin out the way a real room's do (early reflections arrive sparsely and
#: get denser), gains fall roughly as 1/t, and the polarity alternates because a
#: reflection off a boundary is not a copy of what hit it. The first arrival at
#: ~8 ms puts the walls about 3 m out, which is a room a piano fits in; the last
#: at ~75 ms hands over to the tank before the pattern would start sounding like
#: a discrete echo.
_ER_TAPS_L = ((8.3, 0.62), (12.7, -0.48), (17.1, 0.41), (21.9, -0.33),
              (26.4, 0.29), (31.8, -0.24), (37.2, 0.21), (43.1, -0.18),
              (49.5, 0.15), (56.2, -0.13), (63.4, 0.11), (71.0, -0.09))
_ER_TAPS_R = ((9.7, 0.58), (14.3, -0.45), (18.9, 0.39), (23.6, -0.31),
              (28.7, 0.27), (34.1, -0.23), (39.8, 0.19), (45.9, -0.17),
              (52.4, 0.14), (59.3, -0.12), (66.7, 0.10), (74.5, -0.08))

#: Longest reflection, in ms; the delay line is sized from it.
_ER_MAX_MS = max(t for t, _ in _ER_TAPS_L + _ER_TAPS_R)

_COMB_TUNING = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
_ALLPASS_TUNING = (556, 441, 341, 225)
_STEREO_SPREAD = 23

# The block-vectorised recursion is only exact while a chunk is shorter than
# every delay line: a shorter line would read samples written by the same call,
# which produces silently wrong DSP instead of an error. The right-channel lines
# are `+ _STEREO_SPREAD`, so the left-channel tunings are the minimum.
assert CHUNK <= min(_COMB_TUNING + _ALLPASS_TUNING), "CHUNK exceeds a delay line"

#: Slowest round trip in the tank, in samples: the level a comb still holds
#: after `k` of these is at most `feedback ** k` of what it started with.
LONGEST_LINE = max(_COMB_TUNING) + _STEREO_SPREAD

#: Amplitude the tail must fall below before `AudioEngine` may stop calling
#: `process()` at all. -180 dBFS: an int16 sample cannot represent anything
#: below -90.3 dBFS (1/32768), so even after the rest of the chain -- eight
#: combs summed, four allpasses whose loop gain of 0.5 bounds their L1 gain at
#: 4 each, and `wet` <= 0.4, together under 2**10 -- what the gate discards is
#: three orders of magnitude below the smallest number the output format has.
TAIL_FLOOR = 1e-9

#: Ceiling on the comb feedback. At exactly 1.0 the tank never decays and
#: `tail_frames` divides by `log(1.0)`, i.e. by zero, *on the audio thread*;
#: above it the recursion diverges and the bound comes out negative, which makes
#: `AudioEngine`'s idle gate chop the tail instead of waiting for it. Room and
#: Hall sit at 0.874 and 0.946, so this only ever catches a mistyped preset.
MAX_FEEDBACK = 0.995

#: Preset coefficients, float32 like every buffer they are multiplied with: a
#: Python float in the comb/wet path is one `np.float32()` away from promoting a
#: whole block to float64 (and doubling its memory traffic) the day numpy's
#: scalar promotion rules move again. The values are unchanged.
#: `early` is the level of the reflection pattern against the dry signal. The
#: taps are close to incoherent, so their combined RMS gain is about
#: sqrt(sum(g**2)) = 1.08: an `early` of 0.22 therefore lands the pattern ~13 dB
#: under the direct sound, which is where a pair of microphones a couple of
#: metres from a piano would find it. Push it much past 0.35 and the room starts
#: arriving before the note does.
PRESETS = {
    "off":  dict(roomsize=np.float32(0.5), damp=np.float32(0.5),
                 wet=np.float32(0.0), early=np.float32(0.0)),
    "room": dict(roomsize=np.float32(0.62), damp=np.float32(0.55),
                 wet=np.float32(0.24), early=np.float32(0.24)),
    "hall": dict(roomsize=np.float32(0.88), damp=np.float32(0.32),
                 wet=np.float32(0.40), early=np.float32(0.30)),
}


class _DelayLine:
    """A ring buffer read and written one block at a time.

    `_Comb` and `_Allpass` differ only in what they do between the read and the
    write, so the wrap-around arithmetic -- the part that is easy to get subtly
    wrong in two places -- lives here once. The two extra method calls per block
    cost about 6 us per 256-frame block on the path where the reverb is running
    at all (190 -> 196 us, 0.1 % of the block's budget); `AudioEngine` no longer
    calls any of it on silence, which is where the 190 us went.
    """

    __slots__ = ("buf", "idx", "size")

    def __init__(self, size: int) -> None:
        self.buf = np.zeros(size, dtype=np.float32)
        self.idx = 0
        self.size = size     # kept out of `buf.size`: this is a hot inner loop

    def _read(self, n: int) -> np.ndarray:
        """The next `n` samples out of the line, oldest first (a fresh array)."""
        i, end = self.idx, self.idx + n
        if end <= self.size:
            return self.buf[i:end].copy()
        return np.concatenate((self.buf[i:], self.buf[: end - self.size]))

    def _write(self, block: np.ndarray) -> None:
        """Store `block` where `_read` just looked, and step the cursor past it."""
        i, size = self.idx, self.size
        end = i + block.size
        if end <= size:
            self.buf[i:end] = block
        else:
            split = size - i
            self.buf[i:] = block[:split]
            self.buf[: end - size] = block[split:]
        # `end < 2 * size` always (a block never exceeds a line), so this is the
        # modulo without the divide.
        self.idx = end if end < size else end - size


class _TapDelay:
    """One ring buffer read at several fixed delays at once.

    `_DelayLine` is not this: it reads at exactly one delay, its own length,
    which is what a comb needs and what a reflection pattern cannot use. Here the
    write is the same but a read names how far back it wants to look, so a dozen
    taps share one buffer instead of needing a dozen.

    Every tap is at least `CHUNK` samples back (the shortest is ~8 ms = 366
    frames), so like the combs this only ever reads samples written by an
    earlier chunk -- the vectorised block write cannot race its own reads.
    """

    __slots__ = ("buf", "idx", "size")

    def __init__(self, size: int) -> None:
        self.buf = np.zeros(size, dtype=np.float32)
        self.idx = 0
        self.size = size

    def push(self, block: np.ndarray) -> None:
        """Append `block`; `idx` ends up one past its last sample."""
        i, size = self.idx, self.size
        end = i + block.size
        if end <= size:
            self.buf[i:end] = block
        else:
            split = size - i
            self.buf[i:] = block[:split]
            self.buf[: end - size] = block[split:]
        self.idx = end if end < size else end - size

    def tap(self, delay: int, n: int) -> np.ndarray:
        """The `n` samples that sat `delay` frames before the block just pushed."""
        start = self.idx - n - delay
        while start < 0:
            start += self.size
        end = start + n
        if end <= self.size:
            return self.buf[start:end]
        return np.concatenate((self.buf[start:], self.buf[: end - self.size]))

    def clear(self) -> None:
        self.buf[:] = 0.0


class _Comb(_DelayLine):
    __slots__ = ("store",)

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.store = np.float32(0.0)

    def process(self, x: np.ndarray, feedback: np.float32,
                damp: np.float32) -> np.ndarray:
        read = self._read(x.size)

        # One-pole damping approximated by an exact 2-tap FIR (unity DC gain,
        # so the feedback loop stays as stable as the original).
        prev = np.empty_like(read)
        prev[0] = self.store
        prev[1:] = read[:-1]
        self.store = read[-1]
        filtered = (np.float32(1.0) - damp) * read + damp * prev

        self._write(x + feedback * filtered)
        return read


class _Allpass(_DelayLine):
    __slots__ = ()

    def process(self, x: np.ndarray,
                feedback: np.float32 = np.float32(0.5)) -> np.ndarray:
        read = self._read(x.size)
        out = read - x
        self._write(x + feedback * read)
        return out


class Reverb:
    def __init__(self, preset: str = "room",
                 samplerate: int = 44100) -> None:
        self.samplerate = samplerate
        er_len = int(_ER_MAX_MS * 1e-3 * samplerate) + 2 * CHUNK + 2
        self._er = _TapDelay(er_len)
        self._er_l = tuple((max(CHUNK, int(round(t * 1e-3 * samplerate))),
                            np.float32(g)) for t, g in _ER_TAPS_L)
        self._er_r = tuple((max(CHUNK, int(round(t * 1e-3 * samplerate))),
                            np.float32(g)) for t, g in _ER_TAPS_R)
        self._combs_l = [_Comb(s) for s in _COMB_TUNING]
        self._combs_r = [_Comb(s + _STEREO_SPREAD) for s in _COMB_TUNING]
        self._aps_l = [_Allpass(s) for s in _ALLPASS_TUNING]
        self._aps_r = [_Allpass(s + _STEREO_SPREAD) for s in _ALLPASS_TUNING]
        self.set_preset(preset)

    def set_preset(self, preset: str) -> None:
        """Switch presets. Audio-thread only (see `AudioEngine.set_reverb`)."""
        p = PRESETS.get(preset, PRESETS["room"])
        was_enabled = getattr(self, "enabled", False)
        self.enabled = bool(p["wet"] > 0.0)
        # Clamped into [0, MAX_FEEDBACK]: a preset that asks for a roomsize of
        # 1.0715 or more wants a comb that never decays, and the `log(feedback)`
        # below runs on the audio thread -- a ZeroDivisionError there is a dead
        # callback, not a warning. A negative roomsize is nonsense too.
        self.feedback = np.float32(
            min(max(float(p["roomsize"]) * 0.28 + 0.70, 0.0), MAX_FEEDBACK))
        self.damp = np.float32(p["damp"] * 0.45)
        self.wet = np.float32(p["wet"])
        self.early = np.float32(p.get("early", 0.0))
        self.name = preset if preset in PRESETS else "room"
        #: Frames of silent input after which this preset's tail is provably
        #: below `TAIL_FLOOR`, so processing it is arithmetic on nothing. The
        #: bound is the bare comb recursion: the damper is a unity-DC-gain 2-tap
        #: average (|H| <= 1) and the allpasses are passive, so neither can make
        #: the tank decay any *slower* than `feedback` per `LONGEST_LINE`.
        #: 5.7 s for Room, 14.0 s for Concert Hall.
        self.tail_frames = (
            0 if not self.enabled or not 0.0 < self.feedback < 1.0 else
            int(math.ceil(math.log(TAIL_FLOOR) / math.log(float(self.feedback))
                          * LONGEST_LINE))
            # The reflection pattern is finite and sits in front of the tank, so
            # it can only ever add its own length to how long the room keeps
            # answering after the last note.
            + int(math.ceil(_ER_MAX_MS * 1e-3 * getattr(self, "samplerate",
                                                        44100))))
        if self.enabled and not was_enabled:
            # While disabled `process()` returns early, so the delay lines keep
            # (rather than decay) whatever they held when reverb was switched
            # off. Start from silence instead of recirculating that ghost tail.
            self.reset()

    def reset(self) -> None:
        """Clear the delay lines. Audio-thread only, like `set_preset`.

        Deliberately allocation-free (no list concatenation): `AudioEngine` calls
        this from inside the audio callback when a tail has decayed away.
        """
        for lines in (self._combs_l, self._combs_r):
            for c in lines:
                c.buf[:] = 0.0
                c.store = np.float32(0.0)
        for lines in (self._aps_l, self._aps_r):
            for a in lines:
                a.buf[:] = 0.0
        self._er.clear()

    def process(self, stereo: np.ndarray) -> None:
        """Add the wet signal into `stereo` (shape (frames, 2)) in place."""
        if not self.enabled:
            return
        total = stereo.shape[0]
        pos = 0
        while pos < total:
            n = min(CHUNK, total - pos)
            block = stereo[pos:pos + n]

            # The reflection pattern first, and *into* the block: the tank is fed
            # from what comes back off the walls as well as from the instrument,
            # which is the whole reason a real tail sounds like it belongs to the
            # room the direct sound is in.
            if self.early > 0.0:
                dry = (block[:, 0] + block[:, 1]) * np.float32(0.5)
                self._er.push(dry)
                er_l = np.zeros(n, dtype=np.float32)
                er_r = np.zeros(n, dtype=np.float32)
                for delay, gain in self._er_l:
                    er_l += self._er.tap(delay, n) * gain
                for delay, gain in self._er_r:
                    er_r += self._er.tap(delay, n) * gain
                block[:, 0] += self.early * er_l
                block[:, 1] += self.early * er_r

            mono = (block[:, 0] + block[:, 1]) * np.float32(0.5 * 0.015)

            wet_l = np.zeros(n, dtype=np.float32)
            wet_r = np.zeros(n, dtype=np.float32)
            for c in self._combs_l:
                wet_l += c.process(mono, self.feedback, self.damp)
            for c in self._combs_r:
                wet_r += c.process(mono, self.feedback, self.damp)
            for a in self._aps_l:
                wet_l = a.process(wet_l)
            for a in self._aps_r:
                wet_r = a.process(wet_r)

            block[:, 0] += self.wet * wet_l
            block[:, 1] += self.wet * wet_r
            pos += n
