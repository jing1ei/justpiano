# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Just Piano menu bar app."""

import glob
import os
import re
import sys

from PyInstaller.utils.hooks import collect_all

# Single source of truth for the version: justpiano/__init__.py.
_init_py = os.path.join(SPECPATH, "justpiano", "__init__.py")
with open(_init_py, encoding="utf-8") as _fh:
    _match = re.search(r'^__version__ = "([^"]+)"', _fh.read(), re.M)
if not _match:
    raise SystemExit(f"JustPiano.spec: no __version__ found in {_init_py}")
VERSION = _match.group(1)

datas, binaries, hiddenimports = [], [], []
packages = ["sounddevice", "rtmidi", "mido", "soundfile"]
if sys.platform == "darwin":
    packages.append("rumps")
for package in packages:
    # Every one of these is a hard requirement; a failed collection means the
    # bundle would silently ship without PortAudio, _rtmidi or the rumps datas.
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:
        raise SystemExit(
            f"JustPiano.spec: collect_all({package!r}) failed: {exc}\n"
            "Install requirements.txt into the build environment first."
        )
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

hiddenimports += ["mido.backends.rtmidi", "justpiano"]

# The muted menu bar image is loaded from disk at runtime (tray.asset_path), so
# unlike assets/icon.icns -- which BUNDLE bakes into the app -- it has to be
# shipped as data. Missing it costs the muted icon (the app falls back to a 🔇
# title), which is not worth shipping quietly: `build_app.sh` renders both images
# with `python -m tools.make_icon` before it gets here.
_muted_icon = os.path.join(SPECPATH, "assets", "icon-muted.png")
if not os.path.isfile(_muted_icon):
    raise SystemExit("JustPiano.spec: assets/icon-muted.png is missing.\n"
                     "Run `python3 -m tools.make_icon` first.")
datas += [(_muted_icon, "assets"), (os.path.join(SPECPATH, "assets", "icon.png"), "assets")]

# Ship both complete packs, including the manifests used for discovery and
# attribution. A FLAC-only bundle silently falls back to synthesized sounds.
for pack, expected in (("salamander", 212), ("uprightkw", 66)):
    folder = os.path.join(SPECPATH, "assets", "samples", pack)
    manifest = os.path.join(folder, "pack.json")
    recordings = sorted(glob.glob(os.path.join(folder, "*.flac")))
    if not os.path.isfile(manifest) or len(recordings) != expected:
        raise SystemExit(
            f"JustPiano.spec: {pack} needs pack.json and {expected} FLAC files "
            f"(found {len(recordings)}). Restore assets/samples before building.")
    datas += [(f, os.path.join("assets", "samples", pack))
              for f in [manifest, *recordings]]
datas += [(os.path.join(SPECPATH, "README.md"), ".")]
datas += [(os.path.join(SPECPATH, "docs", "DEVELOPMENT.md"), "docs")]
datas += [(path, os.path.join("docs", "screenshots"))
          for path in glob.glob(os.path.join(SPECPATH, "docs", "screenshots", "*.png"))]

# BUNDLE bakes assets/icon.icns into the .app, and nothing in this spec creates
# it: `build_app.sh` renders assets/icon.png with `python -m tools.make_icon` and
# converts it with sips + iconutil, both of which are macOS-only. Without this
# guard a bare `pyinstaller JustPiano.spec` gets all the way through Analysis and
# COLLECT before BUNDLE fails on a missing file -- and on a non-Mac it fails even
# more obscurely, since BUNDLE is a no-op there. Same friendly failure as the
# muted icon above, for the same reason.
_app_icon = os.path.join(SPECPATH, "assets", "icon.icns")
if sys.platform == "darwin" and not os.path.isfile(_app_icon):
    raise SystemExit("JustPiano.spec: assets/icon.icns is missing.\n"
                     "Run `./build_app.sh`, which renders assets/icon.png with "
                     "`python3 -m tools.make_icon` and converts it with "
                     "sips + iconutil.")

a = Analysis(
    [os.path.join(SPECPATH, "main.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "PIL", "pandas", "pytest", "IPython"]
             + (["tkinter", "justpiano.windows"] if sys.platform == "darwin"
                else ["rumps", "AppKit", "Foundation", "objc", "justpiano.tray"]),
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JustPiano",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(SPECPATH, "assets", "icon.ico") if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="JustPiano",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JustPiano.app",
        # The absolute path the guard above checked: a relative one would be resolved
        # against the working directory, so `pyinstaller path/to/JustPiano.spec` from
        # elsewhere could pass the check and then fail in BUNDLE (or the reverse).
        icon=_app_icon,
        bundle_identifier="com.justpiano.app",
        version=VERSION,
        info_plist={
            "LSUIElement": True,               # menu bar only, no Dock icon
            "CFBundleName": "Just Piano",
            "CFBundleDisplayName": "Just Piano",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # Without this purpose string macOS never shows the Automation prompt,
            # so "Open at Login" (which drives System Events) can only ever fail.
            "NSAppleEventsUsageDescription": (
                "Just Piano uses System Events to add or remove itself from your "
                "login items."
            ),
        },
    )
