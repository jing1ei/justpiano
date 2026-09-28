"""Windows CI: real Tk controls with an isolated home and offline audio stream."""
import os
import gc
import tempfile
import time
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory(prefix='justpiano-win-') as home:
        os.environ['JUSTPIANO_HOME'] = home
        import tkinter as tk
        from justpiano.windows import PianoWindow
        from justpiano import windows
        from justpiano.synth import AudioEngine
        from justpiano.midi_in import MidiInput
        from justpiano.recorder import export_midi, export_wav
        def start(engine, device=None):
            engine.stream = None
            engine.error = None
            return True
        root = tk.Tk()
        with patch.object(AudioEngine, 'start', start), \
             patch.object(MidiInput, 'list_ports', return_value=[]):
            app = PianoWindow(root)
            deadline = time.monotonic() + 120
            while not app.bank.ready and not app.bank.error and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            assert app.bank.ready, app.bank.error
            app.recorder.start()
            app.message(0x90, 60, 90)
            assert abs(app.engine.render(4096)).max() > 0
            app.message(0x80, 60, 0)
            app.recorder.stop()
            for label, value in windows.REVERBS.items():
                app.reverb.set(label)
                app.pick_reverb()
                assert app.settings['reverb'] == value
            export_midi(app.recorder.snapshot(), os.path.join(home, 'take.mid'))
            export_wav(app.recorder.snapshot(), app.bank, os.path.join(home, 'take.wav'), reverb='off')
            root.update()
            app.toggle_mute()
            assert app.engine.muted
            app.panic()
            app.recorder.discard()
            app.recorder.session.clear()
            app.quit()
            assert app.closed
            # Windows cannot delete an open memory-mapped cache file.
            del app
            gc.collect()
    print('Windows Tk startup, controls, note playback, capture, export and shutdown passed.')


if __name__ == '__main__':
    main()
