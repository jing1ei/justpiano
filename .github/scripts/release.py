"""Publish verified packages as an immutable GitHub release.

Usage: python .github/scripts/release.py DIR [--dry-run]

DIR must contain exactly the files produced by `tools/package_release.py`
plus their SHA256SUMS.txt. Each version is published once; a release that is
already public is never replaced or re-tagged. Ship again by bumping
`justpiano/__init__.py`.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from justpiano import __version__ as VERSION  # noqa: E402
from tools.package_release import RELEASE_FILES  # noqa: E402
from tools.release_metadata import digest, version_info  # noqa: E402

TAG = 'v' + VERSION
CHECKSUMS = 'SHA256SUMS.txt'
NOTES = (
    'Just Piano {version}.\n\n'
    '| Computer | Download |\n'
    '|---|---|\n'
    '| Mac with Apple silicon (M1 or later) | `JustPiano-{version}-macOS-arm64.dmg` |\n'
    '| Mac with an Intel processor | `JustPiano-{version}-macOS-x64.dmg` |\n'
    '| Windows 10 or later, 64-bit | `JustPiano-{version}-Windows-x64.exe` |\n\n'
    'macOS builds are ad-hoc signed and not notarized. `{checksums}` covers every asset; '
    'the source archive contains the tracked repository. See the README for installation, '
    'requirements and audio sample credits.'
)


def validate(directory: Path, expected_files=RELEASE_FILES) -> dict:
    """Return `{filename: sha256}` for a complete, untampered release directory."""
    sums = directory / CHECKSUMS
    if not sums.is_file():
        raise ValueError(f'Missing {CHECKSUMS} in {directory}')
    manifest = {}
    for line in sums.read_text(encoding='utf-8').splitlines():
        checksum, separator, name = line.partition('  ')
        if not separator or Path(name).name != name or name in manifest:
            raise ValueError(f'Malformed checksum line: {line!r}')
        manifest[name] = checksum
    if set(manifest) != set(expected_files):
        raise ValueError(f'{CHECKSUMS} must list exactly {sorted(expected_files)}; '
                         f'found {sorted(manifest)}')
    present = {path.name for path in directory.iterdir()}
    if present != set(expected_files) | {CHECKSUMS}:
        raise ValueError(f'Unexpected or missing files in {directory}: {sorted(present)}')
    for name, checksum in manifest.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Invalid release asset: {name}')
        if digest(path) != checksum:
            raise ValueError(f'Checksum mismatch: {name}')
    manifest[CHECKSUMS] = digest(sums)
    return manifest


def gh(*args, payload=None):
    result = subprocess.run(['gh', *args], input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True)
    return result.stdout


def api(endpoint, method='GET', payload=None):
    args = ['api', endpoint, '--method', method]
    if payload is not None:
        args += ['--input', '-']
    text = gh(*args, payload=payload)
    return json.loads(text) if text.strip() else None


def pages(endpoint):
    return [item for page in json.loads(gh('api', endpoint, '--paginate', '--slurp')) for item in page]


def tag_commit(prefix, tag):
    """Commit a tag resolves to (following annotated tags), or None if absent."""
    for ref in api(f'{prefix}/git/matching-refs/tags/{tag}') or []:
        if ref['ref'] == f'refs/tags/{tag}':
            target = ref['object']
            while target['type'] == 'tag':
                target = api(f"{prefix}/git/tags/{target['sha']}")['object']
            return target['sha']
    return None


def publish(directory, sha, repo, prerelease, notes=''):
    expected = validate(directory)
    prefix = f'repos/{repo}'

    def require_tag_at_build():
        tagged = tag_commit(prefix, TAG)
        if tagged not in (None, sha):
            raise ValueError(f'{TAG} points at {tagged}, not at the built commit {sha}')

    require_tag_at_build()
    releases = [r for r in pages(f'{prefix}/releases?per_page=100') if r['tag_name'] == TAG]
    if any(not r['draft'] for r in releases):
        raise ValueError(f'{TAG} is already published; bump justpiano/__init__.py to release again')
    if len(releases) > 1:
        raise ValueError(f'Multiple draft releases use {TAG}; delete the extras and retry')
    body = notes or NOTES.format(version=VERSION, checksums=CHECKSUMS)
    body += f'\n\nBuilt from `{sha}`.\n'
    metadata = {'name': f'Just Piano {VERSION}', 'body': body, 'draft': True,
                'prerelease': prerelease, 'target_commitish': sha, 'tag_name': TAG}
    if releases:   # a draft left by a failed earlier attempt
        release = api(f"{prefix}/releases/{releases[0]['id']}", 'PATCH', metadata)
    else:
        release = api(f'{prefix}/releases', 'POST', metadata)
    gh('release', 'upload', TAG, '--repo', repo, '--clobber',
       *(str(directory / name) for name in sorted(expected)))
    # Check what GitHub stored, including resumed uploads, before going public.
    with tempfile.TemporaryDirectory() as temporary:
        args = ['release', 'download', TAG, '--repo', repo, '--dir', temporary]
        for name in sorted(expected):
            args += ['--pattern', name]
        gh(*args)
        for name, checksum in expected.items():
            if digest(Path(temporary) / name) != checksum:
                raise ValueError(f'Uploaded checksum mismatch: {name}')
    for asset in pages(f"{prefix}/releases/{release['id']}/assets?per_page=100"):
        if asset['name'] not in expected:
            api(f"{prefix}/releases/assets/{asset['id']}", 'DELETE')
    # The release is created on target_commitish; GitHub makes v<version> point
    # at that commit. require_tag_at_build() (run before and after the upload)
    # already blocks a tag that pointed elsewhere, so no force-update of an
    # existing tag and no post-publication failure after users could see it.
    api(f"{prefix}/releases/{release['id']}", 'PATCH',
        {'draft': False, 'make_latest': 'false' if prerelease else 'true'})
    print(f'https://github.com/{repo}/releases/tag/{TAG}')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--dry-run', action='store_true', help='validate the directory only')
    args = parser.parse_args()
    ref = os.environ.get('GITHUB_REF', '')
    prerelease = version_info(VERSION, ref)
    validate(args.directory)
    if args.dry_run:
        print(f'{args.directory} holds a complete {TAG} release.')
        return
    sha = os.environ['GITHUB_SHA']
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if head != sha:
        raise ValueError(f'Checkout {head} is not the built commit {sha}')
    if ref not in ('refs/heads/main', f'refs/tags/{TAG}'):
        raise ValueError(f'Only main or tag {TAG} may publish, not {ref}')
    publish(args.directory, sha, os.environ['GITHUB_REPOSITORY'], prerelease)


if __name__ == '__main__':
    main()
