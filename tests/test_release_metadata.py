"""A tag or a package from another build must never silently reach a release."""
from pathlib import Path
import tempfile
import unittest

from justpiano import __version__
from tools.release_metadata import TARGETS, package_name, plan, stamp, verify, version_info


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_and_prerelease_tags(self):
        self.assertFalse(version_info('1.2.3', 'refs/tags/v1.2.3'))
        self.assertTrue(version_info('1.2.3rc1', 'refs/tags/v1.2.3rc1'))
        self.assertFalse(version_info('1.2.3', 'refs/heads/main'))
        for version, ref in [('1.2.3', 'refs/tags/v1.2.4'), ('1.2', ''), ('1.2.3; echo bad', '')]:
            with self.assertRaises(ValueError):
                version_info(version, ref)

    def test_plan_decides_publication_once_per_version(self):
        first = plan(__version__, 'refs/heads/main', 'push', tag_exists=False)
        later = plan(__version__, 'refs/heads/main', 'push', tag_exists=True)
        tagged = plan(__version__, f'refs/tags/v{__version__}', 'push', tag_exists=True)
        pr = plan(__version__, 'refs/heads/feature', 'push', tag_exists=False)
        self.assertTrue(first['publish'] and tagged['publish'])
        self.assertFalse(later['publish'] and pr['publish'])

    def test_three_platform_packages_must_share_commit_and_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for target in TARGETS:
                (root / package_name(target)).write_bytes(target.encode())
                stamp(root, target, 'commit-A')
            verify(root, 'commit-A')
            with self.assertRaises(ValueError):
                verify(root, 'commit-B')
            (root / package_name('Windows-x64')).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                verify(root, 'commit-A')

    def test_empty_or_missing_package_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                stamp(root, 'macOS-arm64', 'commit-A')
            (root / package_name('macOS-arm64')).touch()
            with self.assertRaises(ValueError):
                stamp(root, 'macOS-arm64', 'commit-A')


if __name__ == '__main__':
    unittest.main()
