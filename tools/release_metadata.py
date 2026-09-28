"""Validate release versions and bind every uploaded package to its build commit."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

from justpiano import __version__

TARGETS = {'macOS-arm64': 'dmg', 'macOS-x64': 'dmg', 'Windows-x64': 'zip'}


def version_info(version: str, ref: str) -> bool:
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', version):
        raise ValueError(f'Unsupported release version: {version}')
    if ref.startswith('refs/tags/') and ref != 'refs/tags/v' + version:
        raise ValueError(f'Tag {ref} does not match version {version}')
    return bool(re.search(r'(?:a|b|rc)\d+$', version))


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def stamp(directory: Path, target: str, commit: str):
    filename = f'JustPiano-{__version__}-{target}.{TARGETS[target]}'
    artifact = directory / filename
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise ValueError(f'Missing or empty package: {artifact}')
    metadata = dict(target=target, version=__version__, commit=commit,
                    filename=filename, sha256=digest(artifact))
    (directory / f'build-{target}.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')


def verify(directory: Path, commit: str):
    for target, extension in TARGETS.items():
        path = directory / f'build-{target}.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        filename = f'JustPiano-{__version__}-{target}.{extension}'
        expected = dict(target=target, version=__version__, commit=commit,
                        filename=filename, sha256=digest(directory / filename))
        if metadata != expected:
            raise ValueError(f'Build identity or checksum mismatch: {target}')
    print('All three packages match this commit, version and SHA-256.')


def main():
    command = sys.argv[1]
    if command == 'version':
        prerelease = version_info(__version__, os.environ.get('GITHUB_REF', ''))
        output = os.environ.get('GITHUB_OUTPUT')
        if output:
            with open(output, 'a', encoding='utf-8') as stream:
                stream.write(f'prerelease={str(prerelease).lower()}\n')
        print(f'Version {__version__}; prerelease={prerelease}')
    elif command == 'stamp':
        stamp(Path(sys.argv[2]), sys.argv[3], os.environ['GITHUB_SHA'])
    elif command == 'verify':
        verify(Path(sys.argv[2]), os.environ['GITHUB_SHA'])
    else:
        raise ValueError(f'Unknown operation: {command}')


if __name__ == '__main__':
    main()
