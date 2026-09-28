"""Platform routing, dependency separation and compatibility gate contracts."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from justpiano import launcher
from tools.check_macos_compat import check


class PlatformTests(unittest.TestCase):
    def test_launcher_imports_only_the_selected_frontend(self):
        for platform, module in (('win32', 'justpiano.windows'), ('darwin', 'justpiano.tray')):
            calls = []
            frontend = types.SimpleNamespace(main=lambda: calls.append(True))
            with patch.object(sys, 'platform', platform), patch.dict(sys.modules, {module: frontend}):
                launcher.main()
            self.assertEqual(calls, [True])

    def test_windows_paths_use_local_appdata(self):
        from justpiano import config
        source = Path(config.__file__).read_text()
        scope = {'__name__': 'justpiano._config_probe', '__package__': 'justpiano'}
        env = dict(os.environ)
        env.pop('JUSTPIANO_HOME', None)
        env['LOCALAPPDATA'] = os.path.join(tempfile.gettempdir(), 'Local AppData')
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'platform', 'win32'):
            exec(compile(source, config.__file__, 'exec'), scope)
        self.assertEqual(scope['SUPPORT_DIR'], os.path.join(env['LOCALAPPDATA'], 'Just Piano'))
        self.assertNotIn('Library', scope['SUPPORT_DIR'])

    def test_native_gate_rejects_newer_macos_and_wrong_arch(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'binary').write_bytes(b'\xcf\xfa\xed\xfe')
            def output(version):
                return types.SimpleNamespace(stdout=f'Load command 1\n cmd LC_BUILD_VERSION\n minos {version}\n sdk 15.0\nLoad command 2\n cmd LC_SOURCE_VERSION\n version 1500.0\n')
            with patch('tools.check_macos_compat.subprocess.run', return_value=output('11.0')):
                self.assertEqual(check(folder, 'x86_64'), (1, []))
            with patch('tools.check_macos_compat.subprocess.run', return_value=output('12.0')):
                self.assertIn('requires macOS 12.0', check(folder, 'x86_64')[1][0])
            with patch('tools.check_macos_compat.subprocess.run', return_value=types.SimpleNamespace(stdout='')):
                self.assertTrue(check(folder, 'x86_64')[1])

    def test_windows_packaging_keeps_tk_and_excludes_cocoa(self):
        root = Path(__file__).resolve().parents[1]
        hooks = types.ModuleType('PyInstaller.utils.hooks')
        hooks.collect_all = lambda _: ([], [], [])
        calls = {}
        def analysis(*args, **kw):
            calls.update(kw)
            return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
        namespace = dict(SPECPATH=str(root), Analysis=analysis,
                         PYZ=lambda *a, **kw: None, EXE=lambda *a, **kw: None,
                         COLLECT=lambda *a, **kw: None)
        with patch.object(sys, 'platform', 'win32'), patch.dict(sys.modules, {'PyInstaller.utils.hooks': hooks}):
            exec(compile((root/'JustPiano.spec').read_text(), 'JustPiano.spec', 'exec'), namespace)
        self.assertNotIn('tkinter', calls['excludes'])
        self.assertIn('justpiano.tray', calls['excludes'])
        self.assertNotIn('app', namespace)

class WindowsControllerTests(unittest.TestCase):
    def setUp(self):
        # Controller methods need no display. Real Tk integration runs separately
        # on Windows CI, where the desktop and Tcl/Tk are available.
        try:
            from justpiano.windows import PianoWindow
        except ImportError:
            self.skipTest('Tcl/Tk is not installed in this Python runtime')
        from justpiano import recorder
        from justpiano.keyboard import KeyboardController
        self.app = PianoWindow.__new__(PianoWindow)
        self.app.settings = {}
        self.app.recorder = recorder.Recorder()
        self.app.keys = KeyboardController()
        self.app.engine = unittest.mock.Mock()
        self.app.root = unittest.mock.Mock()
        self.app.closed = False
        self.app.busy = False

    def test_midi_note_zero_velocity_and_panic_lights(self):
        a = self.app
        a.message(0x90, 60, 90)
        self.assertTrue(a.keys.lights.is_down(60))
        a.message(0x90, 60, 0)
        self.assertFalse(a.keys.lights.is_down(60))
        a.engine.note_off.assert_called_once_with(60)
        a.message(0x90, 64, 90)
        a.message(0xB0, 123, 0)
        self.assertEqual(len(a.keys.lights), 0)
        self.assertEqual(a.recorder.stats().session_notes, 2)

    def test_output_failure_falls_back_visibly(self):
        a = self.app
        a.outputs = {'7: Interface': 7}
        a.output_choice = unittest.mock.Mock()
        a.output_choice.get.return_value = '7: Interface'
        a.engine.start.side_effect = [False, True]
        a.start_audio()
        self.assertEqual(a.engine.start.call_args_list,
                         [unittest.mock.call(7), unittest.mock.call(None)])
        a.output_choice.set.assert_called_once_with('System Default')

    def test_export_thread_failure_clears_busy(self):
        import queue
        from justpiano import windows
        a = self.app
        a.bank = types.SimpleNamespace(ready=True)
        a.results = queue.SimpleQueue()
        a.settings = dict(windows.config.DEFAULTS)
        a.recorder.start()
        a.recorder.handle(0x90, 60, 88)
        a.recorder.stop()
        with patch.object(windows.filedialog, 'asksaveasfilename', return_value='take.wav'), \
             patch.object(windows.threading.Thread, 'start', side_effect=RuntimeError('no thread')), \
             patch.object(windows.messagebox, 'showerror') as alert:
            a.export('wav', 'take')
        self.assertFalse(a.busy)
        alert.assert_called_once()

    def test_quit_with_session_capture_requires_confirmation(self):
        from justpiano import windows
        a = self.app
        a.recorder.handle(0x90, 60, 88)
        with patch.object(windows.messagebox, 'askyesno', return_value=False) as confirm:
            a.quit()
        self.assertFalse(a.closed)
        confirm.assert_called_once()
        a.root.destroy.assert_not_called()

    def test_take_export_requires_stop_before_save_dialog(self):
        from justpiano import windows
        a = self.app
        a.bank = types.SimpleNamespace(ready=True)
        a.recorder.start()
        a.recorder.handle(0x90, 60, 88)
        with patch.object(windows.messagebox, 'showinfo') as info, \
             patch.object(windows.filedialog, 'asksaveasfilename') as dialog:
            a.export('mid', 'take')
        info.assert_called_once()
        dialog.assert_not_called()
        self.assertTrue(a.recorder.recording)

    def test_loading_instrument_does_not_open_audio_export_dialog(self):
        from justpiano import windows
        a = self.app
        a.bank = types.SimpleNamespace(ready=False)
        a.recorder.start()
        a.recorder.handle(0x90, 60, 88)
        a.recorder.stop()
        with patch.object(windows.messagebox, 'showinfo') as info, \
             patch.object(windows.filedialog, 'asksaveasfilename') as dialog:
            a.export('wav', 'take')
        info.assert_called_once()
        dialog.assert_not_called()

    def test_active_instrument_selection_keeps_current_audio(self):
        from justpiano import windows
        a = self.app
        a.bank = types.SimpleNamespace(ready=True, voicing='grand')
        a.instrument = unittest.mock.Mock()
        a.instrument.get.return_value = 'Grand Piano'
        with patch.object(windows, 'make_bank') as make:
            a.pick_instrument()
        make.assert_not_called()
        a.engine.stop.assert_not_called()

    def test_busy_export_has_a_confirmed_escape_path(self):
        from justpiano import windows
        a = self.app
        a.busy = True
        a.timer = 'timer'
        a.midi = unittest.mock.Mock()
        a.engine.volume = .75
        with patch.object(windows.messagebox, 'showinfo') as info, \
             patch.object(windows.messagebox, 'askyesno', return_value=False) as confirm:
            a.quit()
            self.assertTrue(a.quit_waited)
            confirm.assert_not_called()
            a.quit()
            confirm.assert_called_once()
            self.assertFalse(a.closed)
        with patch.object(windows.messagebox, 'askyesno', return_value=True):
            a.quit()
        self.assertTrue(a.closed)
        a.root.destroy.assert_called_once()

    def test_release_with_control_modifier_does_not_leave_a_note_down(self):
        a = self.app
        a.keys.key_down('a')
        self.assertIn('a', a.keys.computer_notes)
        a.key_up(types.SimpleNamespace(keysym='a', char='\x01'))
        self.assertFalse(a.keys.computer_notes)
