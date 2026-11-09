"""Release links and source delivery must match the built artifact contract."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from justpiano import __version__
from tools import package_release


class ReleaseTests(unittest.TestCase):
    def test_missing_platform_never_publishes_partial_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(FileNotFoundError):
                package_release.package(root)
            self.assertFalse((root / 'downloads').exists())

    def test_stable_names_and_checksums_match_readme_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in package_release.ASSETS:
                (root/name).write_bytes(name.encode())
            def archive(path):
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('JustPiano/main.py', '# source')
                return path
            with patch.object(package_release, 'source_archive', side_effect=archive):
                output = package_release.package(root)
            self.assertEqual({p.name for p in output.iterdir()}, {
                f'JustPiano-{__version__}-macOS-arm64.dmg', f'JustPiano-{__version__}-macOS-x64.dmg',
                f'JustPiano-{__version__}-Windows-x64.exe', f'JustPiano-{__version__}-source.zip', 'SHA256SUMS.txt'})
            for line in (output/'SHA256SUMS.txt').read_text().splitlines():
                digest, name = line.split('  ')
                self.assertEqual(digest, hashlib.sha256((output/name).read_bytes()).hexdigest())
            readme = (Path(__file__).resolve().parents[1]/'README.md').read_text()
            for name in package_release.ASSETS:
                self.assertIn(name, readme)
            with self.assertRaises(FileExistsError):
                package_release.package(root)
