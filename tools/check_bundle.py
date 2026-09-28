"""Run the packaged executable, including PyInstaller's startup hooks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check(executable):
    with tempfile.TemporaryDirectory(prefix='justpiano-bundle-check-') as directory:
        result = Path(directory) / 'result.json'
        env = dict(os.environ, JUSTPIANO_HOME=str(Path(directory)/'home'))
        process = subprocess.run([str(Path(executable).resolve()), '--self-check', str(result)],
                                 env=env, capture_output=True, text=True, timeout=120)
        if process.returncode or not result.exists():
            raise RuntimeError('Packaged app failed startup:\n' + process.stdout + process.stderr)
        data = json.loads(result.read_text())
        required = {'native-imports', 'sample-playback', 'midi-export', 'wav-export'}
        required.update({'status-bar', 'keyboard-open-close', 'native-key-events'}
                        if sys.platform == 'darwin' else
                        {'native-window', 'window-note-capture', 'reverb-controls'})
        if data.get('startup') != 'ok' or data.get('frozen') is not True or not required.issubset(data.get('checks', [])):
            raise RuntimeError(f'Unexpected packaged startup result: {data}\n{process.stdout}\n{process.stderr}')
    print('Packaged executable verified: ' + ', '.join(data['checks']))


if __name__ == '__main__':
    check(sys.argv[1])
