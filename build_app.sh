#!/usr/bin/env bash
#
# Build JustPiano.app (a self-contained, double-clickable menu bar app),
# optionally package it as a .dmg and/or install it into /Applications.
#
#   ./build_app.sh              # build into ./dist/JustPiano.app
#   ./build_app.sh --install    # build, then copy to /Applications and launch
#   ./build_app.sh --dmg        # build, then package dist/JustPiano-<version>.dmg
#   ./build_app.sh --dmg --install
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-python3}"
VENV=".venv"
INSTALL=0
DMG=0
# --help is answered before the Darwin guard below: the flags are worth reading
# on any machine, and a build script that cannot even print its own usage off a
# Mac is just an exit 1 with no explanation.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script must run on macOS." >&2
  exit 1
fi

for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --dmg)     DMG=1 ;;
    -h|--help) ;;   # already handled above
    *)
      echo "Unknown option: $arg (try --help)" >&2
      exit 1 ;;
  esac
done

# ------------------------------------------------------------------ preflight
# Fail before creating a virtualenv if the checkout is incomplete: the previous
# behaviour was a bare "Could not open requirements file" from pip, several
# steps after the real problem, with a half-built .venv left behind.
# The list is every file the build actually needs, module by module rather than a
# representative few: `tray.py` imports all of them, so a missing one (hotkey.py
# was the one a user hit) used to cost a full virtualenv and a PyInstaller run
# before dying on an ImportError.
# Built as a string rather than an array: macOS ships bash 3.2, where expanding
# an empty array under `set -u` is itself a fatal error.
MISSING=""
for f in requirements.txt JustPiano.spec main.py \
         tools/__init__.py tools/make_icon.py \
         justpiano/__init__.py justpiano/__main__.py \
         justpiano/config.py justpiano/hotkey.py justpiano/keyboard.py \
         justpiano/keyboardview.py justpiano/macui.py justpiano/midi_in.py \
         justpiano/recorder.py justpiano/reverb.py justpiano/samplebank.py \
         justpiano/synth.py justpiano/tone.py justpiano/tray.py; do
  [[ -f "$f" ]] || MISSING="${MISSING}  ${f}"$'\n'
done
if [[ -n "$MISSING" ]]; then
  {
    echo "This does not look like a complete Just Piano checkout."
    echo "Missing from $(pwd):"
    printf '%s' "$MISSING"
    echo
    echo "Copy the whole Just Piano folder, then retry."
  } >&2
  exit 1
fi

# ---------------------------------------------------------------- environment
# Guard on the tools we actually use, not on the directory: `python3 -m venv`
# creates "$VENV" before it runs ensurepip, so an interrupt, a full disk or a
# broken Command Line Tools ensurepip leaves a venv behind with no bin/pip.
# A half-built tree is wiped and rebuilt instead of being skipped forever.
if [[ ! -x "$VENV/bin/python" || ! -x "$VENV/bin/pip" ]]; then
  echo "==> Creating virtualenv"
  rm -rf "$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip wheel
fi
echo "==> Installing dependencies"
"$VENV/bin/pip" install --quiet -r requirements.txt
"$VENV/bin/pip" install --quiet "pyinstaller>=6.3"

# ---------------------------------------------------------------------- icon
echo "==> Building app icons"
"$VENV/bin/python" -m tools.make_icon
ICONSET="build/icon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
# iconutil only accepts this exact set of names.
for size in 16 32 128 256 512; do
  sips -z $size $size assets/icon.png \
       --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z $double $double assets/icon.png \
       --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o assets/icon.icns

# --------------------------------------------------------------------- build
echo "==> Bundling with PyInstaller"
# --noconfirm replaces dist/ output, --clean wipes build/ and the cache.
"$VENV/bin/pyinstaller" --noconfirm --clean JustPiano.spec

APP="dist/JustPiano.app"
[[ -d "$APP" ]] || { echo "Build failed: $APP not found" >&2; exit 1; }

# The muted menu bar image is read from disk at runtime (justpiano/tray.py:
# asset_path), not baked into the executable like assets/icon.icns, so a bundle
# that lost it would only misbehave when the user first clicks Mute - and then
# only by falling back to a 🔇 title. Check the exact path the frozen app looks
# in: PyInstaller puts the spec's datas in Contents/Resources and cross-links
# them into Contents/Frameworks, which is where sys._MEIPASS points.
MUTED_ICON="$APP/Contents/Frameworks/assets/icon-muted.png"
if [[ ! -f "$MUTED_ICON" && ! -f "$APP/Contents/Resources/assets/icon-muted.png" ]]; then
  echo "Build failed: icon-muted.png is not in the bundle" >&2
  echo "   (expected $MUTED_ICON — check the datas entry in JustPiano.spec)" >&2
  exit 1
fi

# Ad-hoc signature: required for the app to launch on Apple Silicon.
# codesign refuses to sign a bundle carrying resource forks or Finder info
# ("resource fork, Finder information, or similar detritus not allowed"), so
# every extended attribute is cleared before sealing. Extended attributes are
# not part of the seal, so running `xattr -cr` on the signed, installed bundle
# (the Gatekeeper workaround in the README) does not invalidate the signature.
echo "==> Signing (ad-hoc)"
xattr -cr "$APP" 2>/dev/null || true
if ! codesign --force --deep --sign - "$APP"; then
  echo "   codesign FAILED — the bundle is unsigned and may refuse to launch" >&2
  echo "   (right-click ▸ Open the first time, or install the Command Line Tools)" >&2
fi

echo "==> Built $APP ($(du -sh "$APP" | cut -f1))"

# ----------------------------------------------------------------------- dmg
if [[ $DMG -eq 1 ]]; then
  # Same single source of truth the spec reads, so the app and the disk image
  # can never claim different versions.
  VERSION="$("$VENV/bin/python" -c 'import justpiano; print(justpiano.__version__)')"
  DMG_PATH="dist/JustPiano-${VERSION}.dmg"
  STAGE="build/dmg"

  echo "==> Packaging $DMG_PATH"
  rm -rf "$STAGE" "$DMG_PATH"
  mkdir -p "$STAGE"
  # A copy of the app plus a symlink to /Applications is what produces the
  # familiar "drag the icon onto the folder" install window.
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"

  # UDZO = zlib-compressed read-only image: the standard format for shipping.
  hdiutil create \
    -volname "Just Piano" \
    -srcfolder "$STAGE" \
    -fs HFS+ \
    -format UDZO \
    -imagekey zlib-level=9 \
    -ov -quiet \
    "$DMG_PATH"
  rm -rf "$STAGE"

  # Ad-hoc signing the image is cosmetic: it seals the .dmg so a corrupted
  # download is detected as such, and nothing more. It does NOT keep Gatekeeper
  # quiet -- an ad-hoc signature is not a Developer ID and the image is not
  # notarized, so anyone who downloads this still gets "Apple could not verify
  # Just Piano is free of malware" and needs right-click > Open once (or
  # `xattr -cr`). Say so when you send the file; the README does.
  codesign --force --sign - "$DMG_PATH" 2>/dev/null || true
  echo "==> Packaged $DMG_PATH ($(du -sh "$DMG_PATH" | cut -f1))"
fi

if [[ $INSTALL -eq 1 ]]; then
  echo "==> Installing to /Applications"
  # `open` would only re-activate an instance that is already running, and on a
  # menu-bar-only (LSUIElement) app that looks exactly like a successful launch
  # while the old build keeps playing. Stop it first, then wait for it to go.
  # `JustPiano`, not `Just Piano`: the process is named after the executable
  # inside the bundle (JustPiano.spec's EXE name), not after the display name.
  # `pkill -x` takes exactly one pattern, so the two-word version was a usage
  # error that `2>/dev/null || true` swallowed whole -- the old build kept
  # running, kept its MIDI port and its audio stream, and `open` on a menu-bar
  # (LSUIElement) app just re-activated it, which looks exactly like a
  # successful install of the new one.
  pkill -x JustPiano 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -x JustPiano >/dev/null 2>&1 || break
    sleep 0.2
  done
  rm -rf "/Applications/JustPiano.app"
  cp -R "$APP" /Applications/
  open "/Applications/JustPiano.app"
  echo "==> Running — look for the 🎹 in your menu bar"
else
  echo
  if [[ $DMG -eq 1 ]]; then
    echo "Next: open the .dmg and drag Just Piano onto the Applications folder."
  else
    echo "Next: open dist/, drag JustPiano.app to /Applications, and launch it."
    echo "Or run: ./build_app.sh --install     (or --dmg to get a disk image)"
  fi
fi
