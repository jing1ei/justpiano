"""Reject a bundle whose native libraries require newer than macOS 11.

Run on macOS after packaging. This inspects load commands, not just Info.plist.
A passing check establishes declared deployment targets, not runtime testing.
"""
import pathlib
import platform
import re
import subprocess
import sys

MAGIC = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
         b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
         b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}


def check(bundle, arch=None):
    arch = arch or platform.machine()
    errors, count = [], 0
    for path in pathlib.Path(bundle).rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as fh:
            if fh.read(4) not in MAGIC:
                continue
        count += 1
        try:
            result = subprocess.run(['otool', '-arch', arch, '-l', str(path)],
                                    check=True, capture_output=True, text=True)
            versions = []
            for command in re.split(r'Load command \d+', result.stdout):
                if 'cmd LC_BUILD_VERSION' in command:
                    versions += re.findall(r'^\s*minos\s+(\d+(?:\.\d+)+)', command, re.M)
                elif 'cmd LC_VERSION_MIN_MACOSX' in command:
                    versions += re.findall(r'^\s*version\s+(\d+(?:\.\d+)+)', command, re.M)
            # LC_VERSION_MIN_MACOSX uses "version"; LC_BUILD_VERSION uses "minos".
            if not versions:
                errors.append(f'{path}: no deployment target for {arch}')
            for version in versions:
                parts = tuple(int(p) for p in version.split('.')[:2])
                if parts > (11, 0):
                    errors.append(f'{path}: requires macOS {version}')
        except subprocess.CalledProcessError as exc:
            errors.append(f'{path}: cannot inspect {arch}: {exc.stderr.strip()}')
    if not count:
        errors.append('No Mach-O binaries found')
    return count, errors


if __name__ == '__main__':
    count, errors = check(sys.argv[1])
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'{count} native binaries declare macOS 11 or earlier ({platform.machine()}).')
