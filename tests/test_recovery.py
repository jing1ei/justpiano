"""Regression checks using isolated settings, caches and export destinations."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mido
import numpy as np

from justpiano import config, recorder, sampled, synth


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="justpiano-regression-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module, key, value in (
            (config, "SUPPORT_DIR", str(self.root)),
            (config, "CACHE_DIR", str(self.root / "cache")),
            (config, "RECORDINGS_DIR", str(self.root / "recordings")),
            (config, "SETTINGS_PATH", str(self.root / "settings.json")),
            (sampled, "CACHE_DIR", str(self.root / "cache")),
        ):
            context = patch.object(module, key, value)
            context.start()
            self.addCleanup(context.stop)

    def test_settings_recover_from_json_types_and_large_numbers(self):
        for value in ([], {}, ["grand"], 10 ** 400, True, None):
            with self.subTest(value=type(value).__name__):
                Path(config.SETTINGS_PATH).write_text(json.dumps(
                    {"voicing": value, "reverb": value, "volume": value}))
                settings = config.Settings()
                for key in ("voicing", "reverb", "volume"):
                    self.assertEqual(settings[key], config.DEFAULTS[key])
                self.assertIsNotNone(settings.error)

    def test_independent_settings_saves_use_distinct_temporary_files(self):
        first, second = config.Settings(), config.Settings()
        replace = os.replace
        paths = []

        def overlapping_save(source, target):
            paths.append(source)
            if len(paths) == 1:
                second["volume"] = 0.5
            replace(source, target)

        with patch.object(config.os, "replace", side_effect=overlapping_save):
            first["volume"] = 1.0
        self.assertEqual(len(set(paths)), 2)
        self.assertIsNone(first.error)
        self.assertIsNone(second.error)
        self.assertEqual(json.loads(Path(config.SETTINGS_PATH).read_text())["volume"], 1.0)

    def small_bank(self):
        bank = sampled.SampledBank("grand")
        # Small but complete key set: cache checks need no expensive decoding.
        bank._notes = {name: np.zeros((8, 2), dtype=np.int16)
                       for name in bank._sources}
        bank._save_cache()
        return bank

    def test_corrupt_sample_cache_is_rejected_without_partial_adoption(self):
        bank = self.small_bank()
        valid = json.loads(Path(bank._index_path).read_text())
        for invalid in ([], None, {**valid, "index": {}},
                        {**valid, "index": {"n:bad": [-1, 5, 2]}},
                        {**valid, "index": {"n:bad": [0, 5, 0]}}):
            with self.subTest(invalid=invalid):
                Path(bank._index_path).write_text(json.dumps(invalid))
                reader = sampled.SampledBank("grand")
                self.assertFalse(reader._load_cache())
                self.assertEqual(reader._notes, {})
        Path(bank._index_path).write_text(json.dumps(valid))
        self.assertTrue(sampled.SampledBank("grand")._load_cache())
        with open(bank._blob_path, "wb") as fh:
            np.save(fh, np.zeros(1920, dtype=np.float32))
        self.assertFalse(sampled.SampledBank("grand")._load_cache())

    def test_decoded_bank_stays_playable_when_cache_write_fails(self):
        bank = sampled.SampledBank("grand")
        with patch.object(bank, "_load_cache", return_value=False), \
             patch.object(bank, "_decode") as decode, \
             patch.object(bank, "_save_cache", side_effect=OSError("disk full")):
            bank.build_blocking()
        decode.assert_called_once()
        self.assertTrue(bank.ready)
        self.assertIn("disk full", bank.error)

    def test_export_releases_sostenuto_on_original_channel(self):
        path = str(self.root / "take.mid")
        recorder.export_midi([(0, 0x92, 60, 90), (0.1, 0xB2, 66, 127),
                              (0.2, 0x82, 60, 0)], path)
        messages = list(mido.MidiFile(path).tracks[0])
        self.assertTrue(any(m.type == "control_change" and m.channel == 2
                            and m.control == 66 and m.value == 0 for m in messages))

    def test_failed_midi_export_preserves_existing_file(self):
        path = self.root / "take.mid"
        path.write_bytes(b"existing take")

        def fail_save(_mid, filename):
            Path(filename).write_bytes(b"partial")
            raise OSError("disk full")

        with patch.object(mido.MidiFile, "save", fail_save):
            with self.assertRaises(OSError):
                recorder.export_midi([(0, 0x90, 60, 90)], str(path))
        self.assertEqual(path.read_bytes(), b"existing take")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_wav_honors_sound_settings_and_cleans_failed_publish(self):
        path = self.root / "take.wav"
        path.write_bytes(b"existing take")
        bank = type("Bank", (), {"ready": True})()
        with patch.object(synth, "AudioEngine") as engine, \
             patch.object(recorder.os, "replace", side_effect=OSError("refused")):
            engine.return_value.render.side_effect = lambda n: np.zeros((n, 2))
            with self.assertRaises(OSError):
                recorder.export_wav([(0, 0x90, 60, 90)], bank, str(path),
                                    key_noise=0.0, resonance=0.2, strike_variation=0.3)
            self.assertEqual(engine.call_args.kwargs["key_noise"], 0.0)
            self.assertEqual(engine.call_args.kwargs["resonance"], 0.2)
            self.assertEqual(engine.call_args.kwargs["strike_variation"], 0.3)
        self.assertEqual(path.read_bytes(), b"existing take")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_packaging_contains_complete_discoverable_packs(self):
        import sys
        import types
        root = Path(__file__).resolve().parents[1]
        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_all = lambda _name: ([], [], [])
        # Execute data collection without starting PyInstaller or needing an icon.
        source = (root / "JustPiano.spec").read_text().split("# BUNDLE bakes")[0]
        namespace = {"SPECPATH": str(root)}
        with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}):
            exec(compile(source, "JustPiano.spec", "exec"), namespace)
        files = {str(Path(destination) / Path(origin).name)
                 for origin, destination in namespace["datas"]}
        for pack in ("salamander", "uprightkw"):
            for source_file in (root / "assets" / "samples" / pack).iterdir():
                if source_file.suffix in (".json", ".flac"):
                    self.assertIn(str(source_file.relative_to(root)), files)
        self.assertIn("README.md", files)


if __name__ == "__main__":
    unittest.main()

class LoaderRecoveryTests(unittest.TestCase):
    def test_thread_start_failure_is_reported_and_can_be_retried(self):
        from justpiano.samplebank import SampleBank
        for bank in (sampled.SampledBank('grand'), SampleBank('rhodes')):
            finished = []
            with patch('threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
                bank.load_or_build_async(on_done=lambda: finished.append(True))
            self.assertIsNone(bank._thread)
            self.assertIn('thread unavailable', bank.error)
            self.assertEqual(finished, [True])
            with patch.object(bank, '_load_cache', return_value=True), \
                 patch.object(SampleBank, '_prune_cache'):
                bank._load_or_build(None, None)
            self.assertTrue(bank.ready)
            self.assertIsNone(bank.error)
