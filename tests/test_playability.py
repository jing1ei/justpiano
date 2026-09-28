"""Pitch, sample-end and local keyboard interaction regressions."""
import unittest
import numpy as np

from justpiano.keyboard import KeyboardController
from justpiano.sampled import SampledBank, resample_audio
from justpiano.synth import AudioEngine, Voice


class PlayabilityTests(unittest.TestCase):
    def test_every_velocity_layer_shares_the_correct_root(self):
        for name in ("grand", "felt", "upright"):
            bank = SampledBank(name)
            for note in range(21, 109):
                rates = []
                for layer in range(len(bank.layer_velocities)):
                    key, rate = bank._map[note, layer]
                    source, correction = bank._sources[key]
                    region = next(r for r in bank.meta["regions"]
                                  if r["file"] == source)
                    self.assertAlmostEqual(
                        correction * rate, 2 ** ((note - region["kc"]) / 12), places=12)
                    rates.append(rate)
                self.assertEqual(len(set(rates)), 1, (name, note))

    def test_resampling_preserves_pitch_stereo_and_rejects_aliases(self):
        rate = 44100
        t = np.arange(rate) / rate
        a = np.column_stack((np.sin(2*np.pi*440*t), .4*np.sin(2*np.pi*440*t)))
        for step in (2 ** (1/12), 2.0, 44100/48000):
            out = resample_audio(a, step)
            self.assertEqual(len(out), round(len(a)/step))
            self.assertTrue(np.isfinite(out).all())
            self.assertLess(float(np.max(np.abs(out[:, 1] - .4*out[:, 0]))), 1e-6)
            spectrum = np.abs(np.fft.rfft(out[:, 0] * np.hanning(len(out))))
            frequency = np.argmax(spectrum) * rate / len(out)
            self.assertLess(abs(frequency - 440*step), 2)
        high = np.column_stack((np.sin(2*np.pi*16000*t),)*2)
        down = resample_audio(high, 2)
        self.assertLess(float(np.sqrt(np.mean(down[64:-64]**2))), .001)

    def test_fractional_reader_retires_before_interpolation_overrun(self):
        bank = type("Bank", (), {})()
        engine = AudioEngine(bank, reverb_preset="off", resonance=0)
        buf = np.ones((32, 2), dtype=np.int16)
        voice = Voice(buf, buf, 1.0, 0.0, 1.0, 1.0, .9, True)
        voice.fpos = 30.2
        engine._voices.append(voice)
        block = engine.render(256)
        self.assertTrue(voice.dead)
        self.assertTrue(np.isfinite(block).all())

    def test_computer_keyboard_chords_repeat_and_focus_cleanup(self):
        events = []
        controller = KeyboardController(
            on_note_on=lambda n, v: events.append(("on", n)),
            on_note_off=lambda n: events.append(("off", n)))
        self.assertTrue(controller.key_down("a"))
        controller.key_down("a")  # OS repeat must not retrigger
        controller.key_down("d")
        self.assertEqual(events, [("on", 60), ("on", 64)])
        self.assertFalse(controller.key_down("z"))
        controller.key_up("a")
        controller.release_inputs()
        controller.release_inputs()
        self.assertEqual(events, [("on", 60), ("on", 64), ("off", 60), ("off", 64)])
        self.assertEqual(controller.computer_notes, {})

    def test_mouse_and_computer_key_share_a_note_until_both_release(self):
        events = []
        c = KeyboardController(on_note_on=lambda n, v: events.append(("on", n)),
                               on_note_off=lambda n: events.append(("off", n)))
        rect = c.keyboard.rect(60)
        c.key_down("a")
        c.mouse_down(rect.center_x, rect.height - 1)
        c.key_up("a")
        self.assertEqual(events, [("on", 60)])
        c.mouse_up()
        self.assertEqual(events, [("on", 60), ("off", 60)])


if __name__ == "__main__":
    unittest.main()
