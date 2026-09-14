"""
Render the same phrase with and without the tuning/variation work, to compare.

    python3 -m tools.ab_phrase demos/          # writes AB-before.wav, AB-after.wav

Not a test and not a demo: `tools/demo_render.py` plays something musical, this
plays something diagnostic. The phrase is built to put pressure on exactly the
two things that changed and nothing else, in three sections:

  1. a repeated note at one fixed velocity, then a trill. Every strike asks for
     the same key at the same speed, so anything you can hear between the two
     files here is `synth.strike_variation` and nothing else. "before" is the
     same twelve samples twelve times.
  2. octaves climbing to the top of the keyboard, held. This is stretch tuning:
     "before" is equal temperament, where partial 2 of the lower key and partial
     1 of the upper are 37 Hz apart by the last octave.
  3. a treble cluster over a bass octave with the damper down, which is where
     several mistuned octaves at once stop being a beat and start being a sound.

"before" is produced by stubbing `tone.stretch_cents` back to zero and forcing
`strike_variation=0`, so it is the real old behaviour rather than a description
of it -- but note it renders its own sample bank, so the first run of each side
pays a full build.
"""

from __future__ import annotations

import os as _os
import tempfile as _tempfile

_os.environ.setdefault("JUSTPIANO_HOME",
                       _os.path.join(_tempfile.gettempdir(), "justpiano-ab"))

import sys
import time

import numpy as np


def build_phrase():
    """(time, status, data1, data2) events; see the module docstring."""
    ev = []
    t = 0.3

    def strike(note, vel, dur, gap=None):
        nonlocal t
        ev.append((round(t, 4), 144, note, vel))
        ev.append((round(t + dur, 4), 128, note, 0))
        t += gap if gap is not None else dur

    # 1. repeated notes, one velocity throughout.
    for _ in range(12):
        strike(72, 84, 0.10, 0.13)
    t += 0.5
    for _ in range(16):
        strike(72, 80, 0.07, 0.085)
        strike(74, 80, 0.07, 0.085)
    t += 0.8

    # 2. octaves up the top of the keyboard, held long enough to beat.
    for low in (72, 79, 84, 91, 96):
        ev.append((round(t, 4), 144, low, 90))
        ev.append((round(t, 4), 144, low + 12, 90))
        ev.append((round(t + 1.5, 4), 128, low, 0))
        ev.append((round(t + 1.5, 4), 128, low + 12, 0))
        t += 1.7
    t += 0.6

    # 3. treble cluster over a bass octave, damper down.
    ev.append((round(t, 4), 176, 64, 127))
    for note, off in ((36, 0.0), (48, 0.05), (76, 0.3), (83, 0.36),
                      (88, 0.42), (95, 0.48), (100, 0.54)):
        ev.append((round(t + off, 4), 144, note, 88))
        ev.append((round(t + off + 3.5, 4), 128, note, 0))
    t += 5.0
    ev.append((round(t, 4), 176, 64, 0))
    t += 1.2

    # 4. the same three notes twice: pedal up, then pedal down. Nothing changes
    #    but the dampers, so the difference is the rest of the instrument.
    for pedal in (0, 127):
        ev.append((round(t, 4), 176, 64, pedal))
        t += 0.25
        for note in (52, 59, 64):
            ev.append((round(t, 4), 144, note, 96))
            ev.append((round(t + 0.45, 4), 128, note, 0))
            t += 0.5
        t += 2.6
    ev.append((round(t, 4), 176, 64, 0))
    t += 2.0
    ev.append((round(t, 4), 176, 123, 0))
    return sorted(ev, key=lambda e: e[0])


def render(mode: str, path: str, voicing: str = "grand") -> None:
    """`mode` is "before" (equal temperament, no variation) or "after"."""
    from justpiano import synth, tone

    if mode == "before":
        # Stub the curve out rather than describe it, and give the bank its own
        # cache key so it cannot be served the stretched blob by mistake.
        tone.stretch_cents = lambda note, voicing=tone.DEFAULT_VOICING: 0.0
        tone._stretch_cache.clear()
        tone.TONE_MODEL_REVISION = tone.TONE_MODEL_REVISION + "-flat"
        # ...and put the room back to a bare Freeverb tail with no reflections.
        from justpiano import reverb
        for preset in reverb.PRESETS.values():
            preset["early"] = np.float32(0.0)

    from justpiano.recorder import export_wav
    from justpiano.samplebank import SampleBank

    original = synth.AudioEngine.__init__

    def forced(self, *args, **kwargs):
        kwargs.setdefault("strike_variation", 1.0 if mode == "after" else 0.0)
        kwargs.setdefault("resonance", 1.0 if mode == "after" else 0.0)
        original(self, *args, **kwargs)

    synth.AudioEngine.__init__ = forced
    try:
        t0 = time.time()
        bank = SampleBank(voicing)
        bank.build_blocking(on_progress=None)
        print(f"{mode}: bank ready in {time.time() - t0:.1f}s")
        export_wav(build_phrase(), bank, path, volume=0.9, reverb="room")
        print(f"{mode}: wrote {path}")
    finally:
        synth.AudioEngine.__init__ = original


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    voicing = sys.argv[2] if len(sys.argv) > 2 else "grand"
    _os.makedirs(out, exist_ok=True)
    # Separate processes would be tidier -- `before` monkeypatches the tone
    # module -- but one run is enough as long as `after` goes first.
    for mode in ("after", "before"):
        render(mode, _os.path.join(out, f"AB-{mode}.wav"), voicing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
