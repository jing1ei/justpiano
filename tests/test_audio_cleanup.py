"""Device errors must not leave the audio stream open."""
import unittest
from unittest.mock import Mock
from justpiano.synth import AudioEngine


class AudioCleanupTests(unittest.TestCase):
    def test_stop_failure_still_closes_the_stream(self):
        engine = AudioEngine(object(), reverb_preset='off')
        stream = Mock()
        stream.stop.side_effect = RuntimeError('device disconnected')
        engine.stream = stream
        engine.stop()
        stream.close.assert_called_once()
        self.assertIsNone(engine.stream)
