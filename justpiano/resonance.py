"""
Sympathetic resonance: the strings whose dampers are off, answering the ones
that were struck.

This is the difference between an instrument and eighty-eight oscillators. A
piano's strings all stand on one bridge over one soundboard, so a struck string
does not sound alone -- it drives every other string that is free to move, and
each of those answers at its own pitch, picking out whatever it has in common
with what was played. It is why pressing the damper pedal on a real piano
changes the sound *before* you play anything else, and why a chord under the
pedal blooms instead of merely lasting longer. Nothing in a per-note sample
bank can produce it, because it is not a property of any one note.

The model is one tuned resonator per string: a comb whose delay is the string's
period, so it rings at that string's pitch and its harmonics, with a damper in
the feedback path so the top of the spectrum dies first the way a real string's
does.

Three deliberate limits, all of them for the same reason -- this has to be free
when it is not sounding and cheap when it is:

  * **Bass and tenor only** (A0-E4). The block-vectorised comb recursion is only
    exact while a delay line is longer than the processing chunk, and at
    `reverb.CHUNK` = 128 that caps the pitch at ~344 Hz. It is also where the
    effect lives: the long, heavy, densely-inharmonic bass strings store far
    more energy than the treble and are most of what you hear answering.
  * **Damper pedal only.** With the pedal up, the strings that are undamped are
    exactly the ones being played, so there is nothing extra to hear. The
    interesting case -- and the one a player recognises instantly -- is the
    pedal going down.
  * **Excluding what is already sounding.** A struck string's own resonance is
    the note; letting its resonator run too would just double it.
"""

from __future__ import annotations

import math

import numpy as np

from . import tone
from .reverb import CHUNK, MAX_FEEDBACK, _Comb

#: Range of strings modelled. The top is set by `CHUNK`: a comb delay shorter
#: than the chunk would read samples written by the same call, which is silently
#: wrong DSP rather than an error. E4 is 134 frames at 44.1 kHz; F4 is 126.
NOTE_LO = 21    # A0
NOTE_HI = 64    # E4

#: How long an undamped string keeps answering, in seconds, and how much faster
#: its high partials go than its fundamental. Real undamped bass strings ring far
#: longer than this; the resonance is a halo behind the note, and one that
#: outlasts the note it belongs to stops reading as the instrument and starts
#: reading as an effect.
T60_S = 1.6
DAMP = 0.28

#: Send level into the bank. A comb's steady-state gain at resonance is
#: 1/(1-feedback), which is 70-odd for the shorter lines, so the input has to
#: come down a long way before forty-four of them are summed -- the number is
#: calibrated by measurement, not derived.
#:
#: This puts the halo 14 dB under the notes driving it. Measured against a
#: repeating five-note figure under the pedal, which is the worst case for a
#: resonant bank feeding on its own output: 0.018 (-8 dB) still does not run
#: away -- the sixth repetition comes back 9.6 dB *below* the second -- so the
#: ceiling here is taste rather than stability. Below about -20 dB the bloom
#: stops being worth the 0.2 ms it costs; above about -11 dB it stops sounding
#: like the same instrument and starts sounding like a second one behind it.
SEND = 0.008

#: Seconds a damper takes to lift or fall. Instant would click; this is roughly
#: what the felt takes anyway.
DAMPER_S = 0.06

#: Fall time of an "immediate" cut (All Sound Off, Panic), matching
#: `synth.DECLICK_TAU`. Duplicated rather than imported: `synth` imports this
#: module, and one shared constant is not worth a cycle.
#:
#: Zeroing the bank outright was tried and measured: it leaves a 0.026 step at
#: the block boundary against a 0.01 limit -- the bank runs 14 dB under the
#: notes, but 14 dB under a chord is still a long way above a click. It is part
#: of the signal, so it fades out with the rest of it.
CUT_TAU = 0.0012

#: Output gain below which a cut is finished and the lines may be dropped.
CUT_FLOOR = 2e-4


class Resonance:
    """The undamped strings. Audio-thread only, exactly like `Reverb`.

    `set_open` publishes a new damper state from the MIDI thread as a single
    array store; the audio thread ramps towards whatever it finds there, so a
    pedal press arriving mid-block can never step the waveform.
    """

    def __init__(self, samplerate: int = 44100) -> None:
        self.samplerate = samplerate
        self.notes = tuple(range(NOTE_LO, NOTE_HI + 1))
        self._combs = []
        self._feedback = []
        for note in self.notes:
            delay = int(round(samplerate / tone.tuned_freq(note)))
            if delay < CHUNK:                    # pragma: no cover - see NOTE_HI
                raise ValueError(f"string {note} is shorter than a chunk")
            self._combs.append(_Comb(delay))
            # feedback**(round trips in T60) = -60 dB
            trips = T60_S * samplerate / delay
            self._feedback.append(np.float32(
                min(10.0 ** (-3.0 / max(trips, 1e-9)), MAX_FEEDBACK)))
        self._damp = np.float32(DAMP)
        #: Target damper state, written by the MIDI thread (0 = damped).
        self._target = np.zeros(len(self.notes), dtype=np.float32)
        #: Where the ramp actually is, owned by the audio thread.
        self._gain = np.zeros(len(self.notes), dtype=np.float32)
        self._step = np.float32(1.0 / max(DAMPER_S * samplerate, 1.0))
        #: How much of the halo reaches the mix; 0.0 turns the bank off without
        #: taking it out of the signal path. Written by the UI thread.
        self.depth = 1.0
        self.active = False
        #: An immediate cut in progress, and how far through it we are.
        self._cutting = False
        self._cut_gain = 1.0
        self._cut_env = np.exp(
            -np.arange(1, CHUNK + 1, dtype=np.float32)
            / np.float32(CUT_TAU * samplerate)).astype(np.float32)

    # ------------------------------------------------------------ MIDI thread
    def set_open(self, sustain: bool, sounding) -> None:
        """Which strings are free to move: none unless the damper pedal is down.

        `sounding` is the set of notes with a live voice; their own strings are
        left out because that resonance is the note itself.
        """
        target = np.zeros(len(self.notes), dtype=np.float32)
        if sustain:
            for i, note in enumerate(self.notes):
                if note not in sounding:
                    target[i] = 1.0
        self._target = target        # one store; no lock (see the class docstring)

    def silence(self) -> None:
        """Fade the whole bank out over `CUT_TAU`, for All Sound Off or a panic.

        Safe from any thread: it publishes two words and leaves the arithmetic --
        and the freeing of the delay lines -- to whichever `process` call picks
        the flag up. `reset` is the hard version, for when no stream is running.
        """
        self._target = np.zeros(len(self.notes), dtype=np.float32)
        self._cutting = True

    def reset(self) -> None:
        """Drop the lines outright. Only safe with no audio thread running."""
        self._target = np.zeros(len(self.notes), dtype=np.float32)
        self._gain[:] = 0.0
        self._cutting = False
        self._cut_gain = 1.0
        for comb in self._combs:
            comb.buf[:] = 0.0
            comb.store = np.float32(0.0)
        self.active = False

    # ----------------------------------------------------------- audio thread
    def process(self, stereo: np.ndarray) -> None:
        """Add what the undamped strings are doing into `stereo`, in place."""
        target = self._target
        gain = self._gain
        if (not self.active and not self._cutting
                and not target.any() and not gain.any()):
            return                      # pedal up and nothing still ringing out

        total = stereo.shape[0]
        pos = 0
        while pos < total:
            n = min(CHUNK, total - pos)
            block = stereo[pos:pos + n]
            drive = (block[:, 0] + block[:, 1]) * np.float32(0.5 * SEND)

            # Walk each damper towards where the pedal says it should be. One
            # step per chunk rather than per sample: 128 frames is 2.9 ms and the
            # whole travel is 60, so the staircase is 20 steps of 0.4 % -- far
            # below what a click needs.
            step = self._step * n
            np.clip(gain + np.clip(target - gain, -step, step), 0.0, 1.0,
                    out=gain)

            # Two buses, because the bank is one bridge but a wide one: the
            # bass strings run to the far end of the soundboard from the tenor,
            # so the halo arrives spread rather than stacked in the middle where
            # the note already is.
            wet_lo = np.zeros(n, dtype=np.float32)
            wet_hi = np.zeros(n, dtype=np.float32)
            half = len(self._combs) // 2
            live = False
            for i, comb in enumerate(self._combs):
                g = float(gain[i])
                if g <= 0.0:
                    if comb.store != 0.0 or comb.buf.any():
                        # Fully damped: let go of the tail rather than keep it to
                        # recirculate the next time the pedal goes down.
                        comb.buf[:] = 0.0
                        comb.store = np.float32(0.0)
                    continue
                live = True
                gf = np.float32(g)
                out = comb.process(drive * gf, self._feedback[i], self._damp)
                if i < half:
                    wet_lo += out * gf
                else:
                    wet_hi += out * gf

            depth = self.depth
            if self._cutting:
                # The same exponential the voices are being cut with, continued
                # across chunks so a cut that starts mid-block does not restart
                # at the boundary.
                env = self._cut_env[:n] * np.float32(self._cut_gain)
                wet_lo = wet_lo * env
                wet_hi = wet_hi * env
                self._cut_gain *= float(self._cut_env[n - 1])
                if self._cut_gain < CUT_FLOOR:
                    self.reset()
                    live = False
            block[:, 0] += (wet_lo * np.float32(0.74 * depth)
                            + wet_hi * np.float32(0.42 * depth))
            block[:, 1] += (wet_lo * np.float32(0.42 * depth)
                            + wet_hi * np.float32(0.74 * depth))
            self.active = live or self._cutting
            pos += n
