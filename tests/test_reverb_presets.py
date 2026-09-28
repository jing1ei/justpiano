"""Audible separation, stability and reset behavior of the reverb choices."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import wave
import numpy as np

from justpiano.config import validate
from justpiano.reverb import PRESETS, Reverb
from justpiano import recorder, synth


class ReverbPresetTests(unittest.TestCase):
    def test_exports_keep_the_selected_room_tail(self):
        bank = type("Bank", (), {"ready": True})()
        with tempfile.TemporaryDirectory() as folder, patch.object(synth, "AudioEngine") as engine:
            engine.return_value.render.side_effect = lambda n: np.zeros((n, 2))
            for name, seconds in (("studio", 3), ("chamber", 4), ("ambient", 8)):
                path = str(Path(folder) / (name + ".wav"))
                recorder.export_wav([(0, 0x90, 60, 88), (1, 0x80, 60, 0)],
                                    bank, path, reverb=name)
                with wave.open(path) as output:
                    self.assertEqual(output.getnframes(), (1 + seconds) * 44100)

    def test_new_rooms_have_distinct_decay_and_stable_stereo_tails(self):
        tails = {}
        responses = {}
        for name in ("studio", "chamber", "ambient"):
            self.assertEqual(validate("reverb", name), (name, None))
            verb = Reverb(name)
            tails[name] = verb.tail_frames
            chunks = []
            for i in range(260):
                block = np.zeros((1024, 2), dtype=np.float32)
                if i == 0:
                    block[0] = .5
                verb.process(block)
                chunks.append(block)
            response = np.concatenate(chunks)
            self.assertTrue(np.isfinite(response).all())
            self.assertLessEqual(np.max(np.abs(response)), .5)
            self.assertGreater(np.max(np.abs(response[1024:, 0] - response[1024:, 1])), .00001)
            self.assertLess(np.mean(response[-44100:] ** 2),
                            np.mean(response[4410:48510] ** 2) * .02)
            verb.reset()
            silence = np.zeros((8192, 2), dtype=np.float32)
            verb.process(silence)
            self.assertFalse(np.any(silence))
            responses[name] = response
        self.assertLess(tails["studio"], Reverb("room").tail_frames)
        self.assertLess(Reverb("room").tail_frames, tails["chamber"])
        self.assertLess(tails["chamber"], Reverb("hall").tail_frames)
        self.assertLess(Reverb("hall").tail_frames, tails["ambient"])
        for first, second in (("studio", "chamber"), ("chamber", "ambient")):
            self.assertGreater(np.linalg.norm(responses[first] - responses[second]), .03)

    def test_every_preset_survives_repeated_switches_and_off(self):
        verb = Reverb("room")
        for _ in range(3):
            for name in PRESETS:
                verb.set_preset(name)
                block = np.full((256, 2), .02, dtype=np.float32)
                verb.process(block)
                self.assertTrue(np.isfinite(block).all())
        verb.set_preset("off")
        verb.set_preset("ambient")
        silence = np.zeros((4096, 2), dtype=np.float32)
        verb.process(silence)
        self.assertFalse(np.any(silence))


if __name__ == "__main__":
    unittest.main()
