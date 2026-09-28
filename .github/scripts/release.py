"""Prepare and publish the complete rolling 1.0.0 release after a main build."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

VERSION = '1.0.0'
TAG = 'v' + VERSION
ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def prepare(source, output, config):
    """Require every platform before creating any release or changing its tag."""
    candidates = [p for p in source.rglob('*') if p.is_file()]
    # Preserve the checksums emitted by platform/source packaging before any
    # filename is normalized. Missing or corrupted inputs stop publication.
    for manifest in candidates:
        if manifest.name in ('SHA256SUMS', 'SHA256SUMS.txt') or manifest.name.endswith('.sha256'):
            for line in manifest.read_text().splitlines():
                checksum, name = line.split('  ', 1)
                name = name.removeprefix('./')
                if Path(name).name != name or digest(manifest.parent / name) != checksum:
                    raise ValueError(f'Input checksum mismatch: {name}')
    selected = {}
    for name, pattern in config['assets'].items():
        matches = [p for p in candidates if p.name == name or fnmatch.fnmatchcase(p.name, pattern)]
        if len(matches) != 1 or matches[0].is_symlink() or matches[0].stat().st_size == 0:
            raise ValueError(f'Expected one nonempty package for {name}; found {matches}')
        selected[name] = matches[0]
    for pattern in config.get('extras', []):
        for path in candidates:
            if fnmatch.fnmatchcase(path.name, pattern):
                if path.name in selected and digest(selected[path.name]) != digest(path):
                    raise ValueError(f'Conflicting additional asset: {path.name}')
                if path.is_symlink() or not path.stat().st_size:
                    raise ValueError(f'Invalid additional asset: {path.name}')
                selected[path.name] = path
    output.mkdir(parents=True, exist_ok=False)
    for name, path in selected.items():
        shutil.copyfile(path, output / name)
    source_name = f"{config['project']}-{VERSION}-source.zip"
    if source_name not in selected:
        subprocess.run(['git', 'archive', '--format=zip', f"--prefix={config['project']}-{VERSION}/",
                        '-o', str((output / source_name).resolve()), 'HEAD'], cwd=ROOT, check=True)
    files = sorted(output.iterdir())
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in files))
    return output


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


def publish(directory, config, sha, repo, notes=''):
    prefix = f'repos/{repo}'
    expected = {}
    for line in (directory / 'SHA256SUMS.txt').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        if Path(name).name != name or digest(directory / name) != checksum:
            raise ValueError(f'Invalid local checksum: {name}')
        expected[name] = checksum
    expected['SHA256SUMS.txt'] = digest(directory / 'SHA256SUMS.txt')
    if set(expected) != {p.name for p in directory.iterdir()}:
        raise ValueError('Unexpected files outside the checksum manifest')

    def current():
        if api(prefix + '/git/ref/heads/main')['object']['sha'] != sha:
            raise ValueError('A newer main commit exists; this build will not replace its release')

    current()
    releases = [r for r in pages(prefix + '/releases?per_page=100') if r['tag_name'] == TAG]
    if len(releases) > 1:
        raise ValueError('Multiple releases use v1.0.0')
    body = notes or config['notes']
    body += f'\n\nBuilt from `{sha}`. Updated automatically after a successful main build.\n'
    metadata = dict(name=VERSION, body=body, draft=True, prerelease=False)
    if releases:
        release = api(prefix + f"/releases/{releases[0]['id']}", 'PATCH', metadata)
    else:
        release = api(prefix + '/releases', 'POST', dict(metadata, tag_name=TAG, target_commitish=sha))
    gh('release', 'upload', TAG, '--repo', repo, '--clobber',
       *(str(directory / name) for name in sorted(expected)))
    # Check what GitHub stored, including resumed uploads, before publishing.
    with tempfile.TemporaryDirectory() as temporary:
        args = ['release', 'download', TAG, '--repo', repo, '--dir', temporary]
        for name in expected:
            args += ['--pattern', name]
        gh(*args)
        for name, checksum in expected.items():
            if digest(Path(temporary) / name) != checksum:
                raise ValueError(f'Uploaded checksum mismatch: {name}')
    current()
    for asset in pages(prefix + f"/releases/{release['id']}/assets?per_page=100"):
        if asset['name'] not in expected:
            api(prefix + f"/releases/assets/{asset['id']}", 'DELETE')
    refs = api(prefix + '/git/matching-refs/tags/' + TAG)
    if any(ref['ref'] == 'refs/tags/' + TAG for ref in refs):
        api(prefix + '/git/refs/tags/' + TAG, 'PATCH', {'sha': sha, 'force': True})
    else:
        api(prefix + '/git/refs', 'POST', {'ref': 'refs/tags/' + TAG, 'sha': sha})
    api(prefix + f"/releases/{release['id']}", 'PATCH', dict(metadata, draft=False, make_latest='true'))
    print(f'https://github.com/{repo}/releases/tag/{TAG}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--notes', type=Path)
    args = parser.parse_args()
    config = json.loads((ROOT / '.github/release.json').read_text())
    output = prepare(args.directory, args.directory.parent / 'release-ready', config)
    if not args.prepare_only:
        sha = os.environ['GITHUB_SHA']
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if actual != sha or os.environ.get('GITHUB_REF') != 'refs/heads/main':
            raise ValueError('Only the current main checkout may publish')
        publish(output, config, sha, os.environ['GITHUB_REPOSITORY'],
                args.notes.read_text() if args.notes else '')


if __name__ == '__main__':
    main()
