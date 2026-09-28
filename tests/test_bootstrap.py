"""Build runtime selection must not reuse an incompatible Python environment."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import bootstrap_env


class BootstrapTests(unittest.TestCase):
    def test_external_wrong_version_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'keep.txt'
            marker.write_text('original')
            with patch.object(bootstrap_env, 'compatible', return_value=False), \
                 patch.object(bootstrap_env.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'Python 3.11'):
                    bootstrap_env.prepare(root, external=True)
            run.assert_not_called()
            self.assertEqual(marker.read_text(), 'original')

    def test_compatible_environment_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(bootstrap_env, 'compatible', return_value=True), \
                 patch.object(bootstrap_env.subprocess, 'run') as run:
                python = bootstrap_env.prepare(root)
            self.assertEqual(python.parent.parent, root)
            self.assertIn('ensurepip', run.call_args.args[0])

    def test_wrong_local_environment_is_backed_up_after_interpreter_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.venv'
            root.mkdir()
            (root / 'original').write_text('3.14')
            valid = Path(directory) / 'python3.11'
            def compatible(path):
                return path == valid or (path.parent.parent == root and (root/'created').exists())
            def run(command, **kwargs):
                self.assertEqual(command[0], str(valid))
                root.mkdir()
                (root/'created').touch()
            with patch.object(bootstrap_env, 'compatible', side_effect=compatible), \
                 patch.object(bootstrap_env.shutil, 'which', return_value=str(valid)), \
                 patch.dict(bootstrap_env.os.environ, {'PYTHON': ''}), \
                 patch.object(bootstrap_env.subprocess, 'run', side_effect=run):
                bootstrap_env.prepare(root)
            backups = list(Path(directory).glob('.venv.previous-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0]/'original').read_text(), '3.14')

    def test_bootstrap_uses_running_311_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / '.venv'
            running = Path(bootstrap_env.sys.executable)
            def compatible(path):
                return path == running or destination.exists()
            def run(command, **kwargs):
                self.assertEqual(command[0], str(running))
                self.assertEqual(command[1:3], ['-m', 'venv'])
                destination.mkdir()
            with patch.dict(bootstrap_env.os.environ, {'PYTHON': ''}), \
                 patch.object(bootstrap_env, 'compatible', side_effect=compatible), \
                 patch.object(bootstrap_env.subprocess, 'run', side_effect=run):
                bootstrap_env.prepare(destination)
