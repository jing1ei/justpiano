#!/usr/bin/env bash
#
# Run Just Piano from source. Creates a local virtualenv on first use.
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-python3}"
VENV=".venv"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Just Piano is a macOS menu bar app and needs macOS to run." >&2
  exit 1
fi

# Fail before building a virtualenv if the checkout is incomplete, rather than
# letting pip report a missing requirements file several steps later. Every
# module `justpiano/__main__.py` ends up importing is listed, not a
# representative few: a missing one (hotkey.py, which tray.py imports, was the
# one a user hit) otherwise costs a whole virtualenv before dying on an
# ImportError. A plain string is used instead of an array because macOS ships
# bash 3.2, where expanding an empty array under `set -u` is itself a fatal
# error.
MISSING=""
for f in tools/bootstrap_env.py requirements.txt constraints-build.txt \
         justpiano/__init__.py justpiano/__main__.py justpiano/launcher.py \
         justpiano/config.py justpiano/hotkey.py justpiano/keyboard.py \
         justpiano/keyboardview.py justpiano/macui.py justpiano/midi_in.py \
         justpiano/recorder.py justpiano/reverb.py justpiano/samplebank.py \
         justpiano/sampled.py justpiano/resonance.py \
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

"$PYTHON_BIN" tools/bootstrap_env.py --venv "$VENV"

# Reinstall whenever requirements.txt changes, and retry after a failed install:
# the stamp is only written once pip has succeeded.
STAMP="$VENV/.requirements.sha256"
WANT="$(shasum -a 256 requirements.txt constraints-build.txt | awk '{print $1}')"
if [[ ! -f "$STAMP" || "$(cat "$STAMP")" != "$WANT" ]]; then
  echo "==> Installing dependencies (this takes a minute the first time)"
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt -c constraints-build.txt
  printf '%s' "$WANT" >"$STAMP"
fi

echo "==> Starting Just Piano — look for the 🎹 in your menu bar"
exec "$VENV/bin/python" -m justpiano
