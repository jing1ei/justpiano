"""Plan releases and bind every uploaded package to its build commit.

Commands (run from the repository root):

  python -m tools.release_metadata plan              decide whether this run publishes
  python -m tools.release_metadata stamp DIR TARGET  record a package's identity
  python -m tools.release_metadata verify DIR        check every package before release
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from justpiano import __version__

TARGETS = {'macOS-arm64': 'dmg', 'macOS-x64': 'dmg', 'Windows-x64': 'exe'}
RELEASE_BRANCH = 'refs/heads/main'
_VERSION = re.compile(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?')
_PRERELEASE = re.compile(r'(?:a|b|rc)\d+$')


def version_info(version: str, ref: str) -> bool:
    """Validate `version` against `ref`; return whether it is a prerelease."""
    if not _VERSION.fullmatch(version):
        raise ValueError(f'Unsupported release version: {version}')
    if ref.startswith('refs/tags/') and ref != 'refs/tags/v' + version:
        raise ValueError(f'Tag {ref} does not match version {version}; '
                         'update justpiano/__init__.py or the tag')
    return bool(_PRERELEASE.search(version))


def plan(version: str, ref: str, event: str, tag_exists: bool) -> dict:
    """Decide what one workflow run does with the packages it builds.

    * pushing tag ``v<version>`` publishes that version;
    * a push or manual run on ``main`` publishes ``v<version>`` the first time
      that version reaches ``main``;
    * everything else (other branches, pull requests, a version that is already
      released) builds, tests and uploads workflow artifacts only.

    A published release is never replaced: shipping again means bumping
    ``justpiano/__init__.py``.
    """
    prerelease = version_info(version, ref)
    tag = 'v' + version
    if event == 'pull_request':
        publish, reason = False, 'pull requests build and test only'
    elif ref.startswith('refs/tags/'):
        publish, reason = True, f'tag {tag} was pushed'
    elif ref == RELEASE_BRANCH and event in ('push', 'workflow_dispatch'):
        if tag_exists:
            publish = False
            reason = f'{tag} is already released; bump justpiano/__init__.py to release again'
        else:
            publish, reason = True, f'{tag} reached main for the first time'
    else:
        publish, reason = False, f'{ref or "this ref"} builds and tests only'
    return {'version': version, 'tag': tag, 'prerelease': prerelease,
            'publish': publish, 'reason': reason}


def remote_tag_exists(tag: str) -> bool:
    result = subprocess.run(['git', 'ls-remote', '--tags', 'origin', f'refs/tags/{tag}'],
                            check=True, capture_output=True, text=True)
    return bool(result.stdout.strip())


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def package_name(target: str) -> str:
    return f'JustPiano-{__version__}-{target}.{TARGETS[target]}'


def stamp(directory: Path, target: str, commit: str):
    filename = package_name(target)
    artifact = directory / filename
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise ValueError(f'Missing or empty package: {artifact}')
    metadata = {'target': target, 'version': __version__, 'commit': commit,
                'filename': filename, 'sha256': digest(artifact)}
    (directory / f'build-{target}.json').write_text(json.dumps(metadata, indent=2),
                                                     encoding='utf-8')


def verify(directory: Path, commit: str):
    for target in TARGETS:
        identity = directory / f'build-{target}.json'
        package = directory / package_name(target)
        if not identity.is_file() or not package.is_file():
            raise ValueError(f'Missing package or build identity for {target}')
        metadata = json.loads(identity.read_text(encoding='utf-8'))
        expected = {'target': target, 'version': __version__, 'commit': commit,
                    'filename': package.name, 'sha256': digest(package)}
        if metadata != expected:
            raise ValueError(f'Build identity or checksum mismatch: {target}')
    print(f'All {len(TARGETS)} packages match this commit, version and SHA-256.')


def _write_outputs(values: dict) -> None:
    output = os.environ.get('GITHUB_OUTPUT')
    if not output:
        return
    with open(output, 'a', encoding='utf-8') as stream:
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            stream.write(f'{key}={text}\n')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ''
    if command == 'plan' and len(argv) == 1:
        ref = os.environ.get('GITHUB_REF', '')
        event = os.environ.get('GITHUB_EVENT_NAME', '')
        tag_exists = ref == RELEASE_BRANCH and remote_tag_exists('v' + __version__)
        result = plan(__version__, ref, event, tag_exists)
        _write_outputs(result)
        action = 'publish' if result['publish'] else 'build only'
        print(f"Version {result['version']} (prerelease={result['prerelease']}): "
              f"{action} - {result['reason']}")
    elif command == 'stamp' and len(argv) == 3:
        stamp(Path(argv[1]), argv[2], os.environ['GITHUB_SHA'])
    elif command == 'verify' and len(argv) == 2:
        verify(Path(argv[1]), os.environ['GITHUB_SHA'])
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
