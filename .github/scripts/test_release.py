"""Offline regressions for the immutable GitHub release publisher."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'publish_release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

from tools import package_release  # noqa: E402
from tools.release_metadata import plan  # noqa: E402


class ReleasePlanTests(unittest.TestCase):
    V = '1.0.0'

    def test_tag_push_publishes_and_must_match_version(self):
        result = plan(self.V, 'refs/tags/v1.0.0', 'push', tag_exists=True)
        self.assertTrue(result['publish'])
        self.assertEqual(result['tag'], 'v1.0.0')
        with self.assertRaises(ValueError):
            plan(self.V, 'refs/tags/v1.0.1', 'push', tag_exists=False)

    def test_main_publishes_once_then_builds_only(self):
        first = plan(self.V, 'refs/heads/main', 'push', tag_exists=False)
        again = plan(self.V, 'refs/heads/main', 'push', tag_exists=True)
        manual = plan(self.V, 'refs/heads/main', 'workflow_dispatch', tag_exists=True)
        self.assertTrue(first['publish'])
        self.assertFalse(again['publish'])
        self.assertFalse(manual['publish'])

    def test_branches_and_pull_requests_never_publish(self):
        self.assertFalse(plan(self.V, 'refs/heads/feature', 'push', False)['publish'])
        self.assertFalse(plan(self.V, 'refs/heads/main', 'pull_request', False)['publish'])
        self.assertFalse(plan(self.V, 'refs/heads/main', 'schedule', False)['publish'])

    def test_prerelease_flag(self):
        self.assertFalse(plan('1.0.0', 'refs/heads/main', 'push', False)['prerelease'])
        rc = plan('1.0.1rc1', 'refs/tags/v1.0.1rc1', 'push', True)
        self.assertTrue(rc['prerelease'])
        self.assertTrue(rc['publish'])


class ReleaseDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def build(self):
        for name in package_release.ASSETS:
            (self.dir / name).write_bytes(name.encode())
        return package_release.package(self.dir)

    def test_complete_directory_validates_with_checksums(self):
        downloads = self.build()
        manifest = release.validate(downloads)
        self.assertIn(release.CHECKSUMS, manifest)
        self.assertEqual(set(manifest), set(package_release.RELEASE_FILES) | {release.CHECKSUMS})

    def test_tampered_or_incomplete_directory_is_rejected(self):
        downloads = self.build()
        (downloads / package_release.ASSETS[0]).write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            release.validate(downloads)
        (downloads / package_release.ASSETS[0]).unlink()
        with self.assertRaises(ValueError):
            release.validate(downloads)

    def test_extra_file_is_rejected(self):
        downloads = self.build()
        (downloads / 'extra.txt').write_text('x')
        with self.assertRaises(ValueError):
            release.validate(downloads)


class PublisherFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.calls = []
        self.existing = None
        for name in package_release.RELEASE_FILES:
            (self.directory / name).write_bytes(name.encode())
        checksums = ''.join(
            f'{release.digest(self.directory / name)}  {name}\n'
            for name in sorted(package_release.RELEASE_FILES))
        (self.directory / release.CHECKSUMS).write_text(checksums)

    def api(self, endpoint, method='GET', payload=None):
        self.calls.append((method, endpoint, payload))
        if '/git/matching-refs/tags/' in endpoint:
            return self.tag_refs
        if method == 'POST' and endpoint.endswith('/releases'):
            self.existing = {'id': 7, 'draft': True, 'tag_name': release.TAG}
            return self.existing
        if method == 'PATCH':
            return {'id': 7, 'draft': payload.get('draft', True), 'tag_name': release.TAG}
        return {}

    def gh(self, *args):
        self.calls.append(('gh', args, None))
        if args[:2] == ('release', 'download'):
            destination = Path(args[args.index('--dir') + 1])
            shutil.copytree(self.directory, destination, dirs_exist_ok=True)
        return ''

    def pages(self, endpoint):
        self.calls.append(('pages', endpoint, None))
        if endpoint.endswith('/releases?per_page=100'):
            return [] if not self.existing else [self.existing]
        if endpoint.endswith('/assets?per_page=100'):
            return [{'name': 'stale.bin', 'id': 99}]
        if '/git/matching-refs/tags/' in endpoint:
            return self.tag_refs
        return []

    def publish(self, existing=None, tag_sha=None):
        """Publish with the pre-existing tag at `tag_sha` (None = no tag yet)."""
        self.existing = existing
        self.tag_refs = [{'ref': f'refs/tags/{release.TAG}',
                          'object': {'type': 'commit', 'sha': tag_sha}}] if tag_sha else []
        with patch.object(release, 'api', side_effect=self.api), \
                patch.object(release, 'pages', side_effect=self.pages), \
                patch.object(release, 'gh', side_effect=self.gh):
            release.publish(self.directory, 'built', 'owner/repo', prerelease=False)

    def test_creates_draft_uploads_verifies_then_publishes(self):
        self.publish()
        last = self.calls[-1]
        self.assertEqual(last[:2], ('PATCH', 'repos/owner/repo/releases/7'))
        self.assertFalse(last[2]['draft'])
        self.assertEqual(last[2]['make_latest'], 'true')
        upload = next(c for c in self.calls if c[0] == 'gh' and c[1][:2] == ('release', 'upload'))
        self.assertIn(release.TAG, upload[1])
        self.assertIn('DELETE', [m for m, _, _ in self.calls if m != 'gh'])

    def test_prerelease_is_not_marked_latest(self):
        self.existing = None
        self.tag_refs = []
        with patch.object(release, 'api', side_effect=self.api), \
                patch.object(release, 'pages', side_effect=self.pages), \
                patch.object(release, 'gh', side_effect=self.gh):
            release.publish(self.directory, 'built', 'owner/repo', prerelease=True)
        self.assertEqual(self.calls[-1][2]['make_latest'], 'false')

    def test_published_release_is_never_replaced(self):
        with self.assertRaisesRegex(ValueError, 'already published'):
            self.publish(existing={'id': 8, 'draft': False, 'tag_name': release.TAG})

    def test_existing_draft_is_reused_and_no_tag_is_forced(self):
        self.publish(existing={'id': 8, 'draft': True, 'tag_name': release.TAG})
        self.assertFalse(any(m == 'POST' and e.endswith('/releases')
                             for m, e, _ in self.calls))
        self.assertFalse(any('/git/refs' in e for m, e, _ in self.calls))
        draft = next(p for m, e, p in self.calls if m == 'PATCH' and '/releases/' in e)
        self.assertEqual(draft['tag_name'], release.TAG)

    def test_wrong_tag_commit_blocks_publication(self):
        with self.assertRaisesRegex(ValueError, 'points at'):
            self.publish(tag_sha='elsewhere')


if __name__ == '__main__':
    unittest.main()
