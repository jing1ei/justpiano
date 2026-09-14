#!/usr/bin/env python3
"""Just Piano launcher (the PyInstaller entry point).

A thin shim over the same startup path as `python3 -m justpiano`, so the two
entry points cannot drift apart.
"""

from justpiano.tray import main

if __name__ == "__main__":
    main()
