"""
Print the tuning curve a voicing is rendered at (not a test — it asserts nothing).

    python3 -m tools.tuning_report              # the default instrument
    python3 -m tools.tuning_report upright      # one named instrument
    python3 -m tools.tuning_report all          # every struck-string voicing

Three columns are worth knowing apart:

  shift    what `tone.stretch_cents` moves the *nominal* f0 by. This is the
           number the renderer uses and it is not what a meter reads.
  meter    where partial 1 actually ends up, in cents from equal temperament --
           `tone.railsback_cents`. This is the column to hold a tuning meter up
           against, and the one to compare with a published Railsback curve.
  beat     partial 2 of this key against partial 1 of the key an octave up, in
           Hz, before and after. This is the whole reason the curve exists: the
           "was" figures are what equal temperament left behind.

The curve is derived from `tone.inharmonicity`, so it follows `_B_VALUES`
without being touched. If the bass here looks flat next to a real piano's
20-30 cent droop, that is `_B_VALUES` being an order of magnitude low in the
bottom octave (3e-5 at A0 against a measured 1e-4 to 4e-4), not the curve.
"""

from __future__ import annotations

import math
import sys

from justpiano import tone

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def name(note: int) -> str:
    return f"{NAMES[note % 12]}{note // 12 - 1}"


def octave_beat(note: int, voicing: str, freq) -> float:
    """Partial 2 of `note` against partial 1 of `note` + 12, in Hz."""
    b_lo = tone.inharmonicity(note, voicing)
    b_hi = tone.inharmonicity(note + 12, voicing)
    return abs(2.0 * freq(note, voicing) * math.sqrt(1.0 + 4.0 * b_lo)
               - freq(note + 12, voicing) * math.sqrt(1.0 + b_hi))


def flat(note: int, voicing: str) -> float:
    return tone.midi_to_freq(note)


def report(voicing: str) -> None:
    if tone.family(voicing) != tone.STRING:
        print(f"\n{voicing}")
        print("  an electric voicing is not stretch-tuned: bending modes, not a "
              "harmonic series, and no `stretch` knob to read")
        return
    print(f"\n{voicing}  (stretch={tone.VOICINGS[voicing]['stretch']})")

    print(f"  {'key':<5}{'B':>10}{'shift c':>9}{'meter c':>9}{'Hz':>11}")
    for note in range(tone.NOTE_MIN, tone.NOTE_MAX + 1, 6):
        print(f"  {name(note):<5}{tone.inharmonicity(note, voicing):10.2e}"
              f"{tone.stretch_cents(note, voicing):9.2f}"
              f"{tone.railsback_cents(note, voicing):9.2f}"
              f"{tone.tuned_freq(note, voicing):11.3f}")

    a4 = (tone.tuned_freq(69, voicing)
          * math.sqrt(1.0 + tone.inharmonicity(69, voicing)))
    print(f"  A4 partial 1 = {a4:.4f} Hz")

    print(f"\n  {'octave':<12}{'was':>9}{'now':>9}   beat, Hz")
    for note in range(tone.NOTE_MIN + 3, tone.NOTE_MAX - 11, 12):
        was = octave_beat(note, voicing, flat)
        now = octave_beat(note, voicing, tone.tuned_freq)
        bar = "#" * min(40, int(now * 4))
        print(f"  {name(note) + '-' + name(note + 12):<12}"
              f"{was:9.2f}{now:9.2f}   {bar}")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else tone.DEFAULT_VOICING
    if which == "all":
        names = list(tone.VOICINGS)
    elif which in tone.VOICINGS:
        names = [which]
    else:
        print(f"unknown voicing {which!r}; have: {', '.join(tone.VOICINGS)}")
        return 2
    for voicing in names:
        report(voicing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
