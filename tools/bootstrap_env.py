"""Prepare the pinned Python 3.11 build environment without global installs."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def compatible(python: Path) -> bool:
    try:
        result = subprocess.run([str(python), '-c',
                                 'import sys; print("%s.%s" % sys.version_info[:2])'],
                                capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and result.stdout.strip() == '3.11'
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare(destination: Path, external=False) -> Path:
    python = destination / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if compatible(python):
        subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True,
                       stdout=subprocess.DEVNULL)
        return python
    if external:
        raise RuntimeError('JUSTPIANO_VENV must contain Python 3.11; the supplied environment was not changed.')

    # Prefer a compatible interpreter already installed on this computer.
    candidates = [os.environ.get('PYTHON'), sys.executable, shutil.which('python3.11'),
                  '/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11',
                  '/opt/homebrew/opt/python@3.11/bin/python3.11',
                  '/usr/local/opt/python@3.11/bin/python3.11']
    interpreter = next((Path(p) for p in candidates if p and compatible(Path(p))), None)
    if interpreter is None:
        cache = Path.home() / 'Library' / 'Caches' / 'Just Piano' / 'build-runtime'
        if os.name == 'nt':
            cache = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Just Piano' / 'build-runtime'
        cache.mkdir(parents=True, exist_ok=True)
        tools = cache / 'tools'
        env = dict(os.environ, PYTHONPATH=str(tools), UV_PYTHON_INSTALL_DIR=str(cache / 'python'))
        uv = [sys.executable, '-m', 'uv']
        if not (tools / 'uv' / '__main__.py').exists():
            print('==> Preparing a private Python 3.11 runtime', flush=True)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet',
                            '--target', str(tools), 'uv>=0.8,<1'], check=True)
        subprocess.run(uv + ['python', 'install', '3.11'], env=env, check=True)
        result = subprocess.run(uv + ['python', 'find', '--managed-python', '3.11'],
                                env=env, check=True, capture_output=True, text=True)
        interpreter = Path(result.stdout.strip())
        if not compatible(interpreter):
            raise RuntimeError('The downloaded interpreter did not report Python 3.11.')

    # Download/verify first. Preserve an incompatible environment for recovery.
    if destination.exists():
        backup = destination.with_name(destination.name + '.previous-' + time.strftime('%Y%m%d-%H%M%S'))
        if backup.exists():
            raise FileExistsError(backup)
        destination.rename(backup)
        print(f'==> Previous environment preserved: {backup}', flush=True)
    subprocess.run([str(interpreter), '-m', 'venv', str(destination)], check=True)
    if not compatible(python):
        raise RuntimeError('Environment creation did not produce Python 3.11.')
    return python


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--venv', default='.venv')
    parser.add_argument('--external', action='store_true')
    args = parser.parse_args()
    try:
        python = prepare(Path(args.venv).absolute(), args.external)
        print(f'==> Using Python 3.11: {python}')
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Could not prepare the build runtime: {exc}')


if __name__ == '__main__':
    main()
