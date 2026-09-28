"""A tag or a package from another build must never silently reach a release."""
from pathlib import Path
import tempfile
import unittest

from justpiano import __version__
from tools.release_metadata import TARGETS, stamp, verify, version_info


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_and_prerelease_tags(self):
        self.assertFalse(version_info('1.2.3', 'refs/tags/v1.2.3'))
        self.assertTrue(version_info('1.2.3rc1', 'refs/tags/v1.2.3rc1'))
        self.assertFalse(version_info('1.2.3', 'refs/heads/main'))
        for version, ref in [('1.2.3', 'refs/tags/v1.2.4'), ('1.2', ''), ('1.2.3; echo bad', '')]:
            with self.assertRaises(ValueError):
                version_info(version, ref)

    def test_three_platform_packages_must_share_commit_and_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for target, ext in TARGETS.items():
                (root/f'JustPiano-{__version__}-{target}.{ext}').write_bytes(target.encode())
                stamp(root, target, 'commit-A')
            verify(root, 'commit-A')
            with self.assertRaises(ValueError):
                verify(root, 'commit-B')
            (root/f'JustPiano-{__version__}-Windows-x64.zip').write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verify(root, 'commit-A')

    def test_empty_or_missing_package_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                stamp(root, 'macOS-arm64', 'commit-A')
            (root/f'JustPiano-{__version__}-macOS-arm64.dmg').touch()
            with self.assertRaises(ValueError):
                stamp(root, 'macOS-arm64', 'commit-A')
