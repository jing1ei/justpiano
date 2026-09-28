"""Verify the frozen entry point, native controls, piano playback and exports."""
import json
from pathlib import Path
import sys
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check(result_path):
    from .sampled import make_bank
    from .synth import AudioEngine
    from .recorder import export_midi, export_wav
    import rtmidi
    import sounddevice
    import soundfile
    result_path = Path(result_path)
    bank = make_bank('grand')
    bank.build_blocking()
    require(bank.ready and bank.stereo, bank.error or 'Recorded grand unavailable')
    engine = AudioEngine(bank, reverb_preset='off')
    engine.note_on(60, 90)
    require(abs(engine.render(4096)).max() > 0, 'No rendered audio')
    events = [(0., 0x90, 60, 90), (.2, 0x80, 60, 0)]
    export_midi(events, str(result_path.with_suffix('.mid')))
    export_wav(events, bank, str(result_path.with_suffix('.wav')), reverb='off')
    checks = ['native-imports', 'sample-playback', 'midi-export', 'wav-export']

    def success():
        result_path.write_text(json.dumps({
            'startup': 'ok', 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': checks,
        }))

    if sys.platform == 'darwin':
        import rumps
        from .tray import JustPianoApp
        from AppKit import NSApplication
        from Foundation import NSDate, NSRunLoop
        NSApplication.sharedApplication()
        app = JustPianoApp()
        def ready(_timer):
            _timer.stop()
            try:
                status = app._nsapp.nsstatusitem
                require(status is not None and status.button() is not None, 'No status bar button')
                require(status.button().window() is not None, 'No status bar window')
                require(app.panel is not None, 'Keyboard panel could not be created')
                opened = app.panel.open()
                # Let native display and responder setup finish on the actual run loop.
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.1))
                require(app.panel.is_open, f'Keyboard panel could not open (initial={opened}, button={status.button().bounds()}, window={status.button().window().frame()})')
                app.panel._view.keyDown_(KeyEvent('a'))
                require(app.keys.computer_notes.get('a') == 60, 'Native key press not dispatched')
                app.panel._view.keyUp_(KeyEvent('a'))
                require(not app.keys.computer_notes, 'Native key release not dispatched')
                app.panel.close()
                require(not app.panel.is_open, 'Keyboard panel did not close')
                checks.extend(['status-bar', 'keyboard-open-close', 'native-key-events'])
                success()
            except Exception as exc:
                result_path.write_text(json.dumps({'startup': 'failed', 'error': str(exc)}))
            finally:
                app.midi.shutdown()
                app.engine.stop()
                app.timer.stop()
                app.panel_timer.stop()
                app.hotkeys.stop()
                rumps.quit_application()
        # before_start callbacks are a set, not an ordered list. Verify on the
        # run loop after all installers, rather than racing panel installation.
        verifier = rumps.Timer(ready, .2)
        verifier.start()
        app.run()
    elif sys.platform == 'win32':
        import tkinter as tk
        from .windows import PianoWindow, REVERBS
        root = tk.Tk()
        app = PianoWindow(root)
        try:
            deadline = time.monotonic() + 60
            while not app.bank.ready and not app.bank.error and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            require(app.bank.ready, app.bank.error or 'Window instrument loading timed out')
            root.update()
            require(root.winfo_viewable(), 'Piano window is not visible')
            app.keys.key_down('a')
            app.keys.key_up('a')
            require(app.recorder.stats().session_notes == 1, 'Window note not recorded')
            for label, value in REVERBS.items():
                app.reverb.set(label)
                app.pick_reverb()
                require(app.settings['reverb'] == value, 'Reverb selection failed')
            checks.extend(['native-window', 'window-note-capture', 'reverb-controls'])
            success()
        finally:
            app.recorder.discard()
            app.recorder.session.clear()
            app.quit()
    else:
        raise RuntimeError('Unsupported packaged platform')


class KeyEvent:
    def __init__(self, key):
        self.key = key

    def charactersIgnoringModifiers(self):
        return self.key

    def modifierFlags(self):
        return 0
