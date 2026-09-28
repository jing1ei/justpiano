"""Select the native frontend without importing another platform's UI."""
import sys


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check':
        from .startup_check import check
        check(sys.argv[2])
        return
    if sys.platform == "darwin":
        from .tray import main as run
    elif sys.platform == "win32":
        from .windows import main as run
    else:
        raise SystemExit("Just Piano supports macOS 11+ and Windows 10+.")
    run()
