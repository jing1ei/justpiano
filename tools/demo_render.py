"""
Offline demo renderer (not a test — it renders and prints, it asserts nothing).

Builds the sample bank, plays a short chord progression + melody through the
same engine the tray app uses, and writes a WAV so you can audition an
instrument without any hardware.

    python3 -m tools.demo_render demo.wav              # the default instrument
    python3 -m tools.demo_render demo.wav rhodes       # one named instrument
    python3 -m tools.demo_render demo.wav all          # every one, side by side
"""

from __future__ import annotations

import os as _os
import tempfile as _tempfile

# Keep the demo away from a real installation's settings/recordings, while
# still reusing its own sample cache between runs.
_os.environ.setdefault("JUSTPIANO_HOME",
                       _os.path.join(_tempfile.gettempdir(), "justpiano-demo"))

import sys
import time

from justpiano import tone
from justpiano.recorder import export_midi, export_wav
from justpiano.samplebank import SampleBank

NOTE_ON, NOTE_OFF, CC = 0x90, 0x80, 0xB0


def build_demo_events():
    """A little ii-V-I in C with a melody on top, plus pedal work."""
    events = []

    def note(t, pitch, dur, vel):
        events.append((t, NOTE_ON, pitch, vel))
        events.append((t + dur, NOTE_OFF, pitch, 0))

    def pedal(t, down):
        events.append((t, CC, 64, 127 if down else 0))

    chords = [
        (0.0, [50, 57, 65, 69], 1.9),   # Dm9
        (2.0, [43, 59, 65, 69], 1.9),   # G13
        (4.0, [48, 55, 64, 71], 3.4),   # Cmaj9
    ]
    for start, pitches, dur in chords:
        pedal(start - 0.02 if start else 0.0, True)
        for i, pitch in enumerate(pitches):
            note(start + i * 0.012, pitch, dur, 62 + i * 4)
        pedal(start + dur, False)

    melody = [
        (0.30, 74, 0.35, 88), (0.70, 72, 0.30, 80), (1.05, 69, 0.55, 84),
        (2.30, 71, 0.35, 92), (2.70, 74, 0.30, 86), (3.05, 77, 0.60, 96),
        (4.30, 76, 0.45, 78), (4.80, 72, 0.40, 70), (5.30, 76, 1.60, 64),
    ]
    for t, pitch, dur, vel in melody:
        note(t, pitch, dur, vel)

    # Dynamic range check: same note, pp -> ff
    for i, vel in enumerate((22, 48, 76, 104, 127)):
        note(7.6 + i * 0.55, 60, 0.45, vel)

    events.sort(key=lambda e: e[0])
    return events


def _stem(wav_path: str) -> str:
    """`wav_path` without a trailing ".wav".

    Only a real ".wav" suffix is stripped: `rsplit(".", 1)` would cut on a dot in
    a parent directory name and drop the sibling files outside the folder that
    was asked for.
    """
    return wav_path[:-4] if wav_path.lower().endswith(".wav") else wav_path


def render(voicing: str, wav_path: str, events) -> None:
    """Build one instrument's bank and render the demo through it."""
    t0 = time.time()
    bank = SampleBank(voicing)
    bank.build_blocking(on_progress=None)
    print(f"{voicing}: bank ready in {time.time() - t0:.2f}s")

    t0 = time.time()
    # Electric pianos are amplified instruments: a concert-hall tail on one is a
    # costume, not a room, so they get the small room the amp would sit in.
    reverb = "hall" if tone.family(voicing) == tone.STRING else "room"
    export_wav(events, bank, wav_path, reverb=reverb, volume=0.85)
    print(f"{voicing}: rendered {wav_path} in {time.time() - t0:.2f}s")


def main() -> int:
    wav_path = sys.argv[1] if len(sys.argv) > 1 else "demo.wav"
    wanted = sys.argv[2] if len(sys.argv) > 2 else tone.DEFAULT_VOICING
    if wanted == "all":
        voicings = list(tone.VOICINGS)
    elif wanted in tone.VOICINGS:
        voicings = [wanted]
    else:
        print(f"unknown instrument {wanted!r}; pick one of "
              f"{', '.join(tone.VOICINGS)} (or 'all')", file=sys.stderr)
        return 2

    events = build_demo_events()
    stem = _stem(wav_path)
    for voicing in voicings:
        render(voicing, wav_path if len(voicings) == 1 else f"{stem}-{voicing}.wav",
               events)

    mid_path = stem + ".mid"
    export_midi(events, mid_path)
    print(f"wrote {mid_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
