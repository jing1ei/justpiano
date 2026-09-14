#!/usr/bin/env bash
#
# Double-click this file in Finder to build Just Piano and install it.
#
# Finder runs a .command in a fresh Terminal window with the *home* directory as
# the working directory, not the folder the file is in -- hence the cd below,
# which uses the script's own path so this keeps working wherever the folder is
# moved to. It hands over to build_app.sh, which is still the thing to run from
# a terminal if you want the other flags (--dmg, --help).
#
# `exec`ing would leave the window on the build's own last line; the wait at the
# bottom is what stops a failure from vanishing behind a closing window, since a
# Terminal set to "close if the shell exited cleanly" does exactly that.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Invoked through `bash` rather than as `./build_app.sh`: the executable bit is
# the one thing about this folder that does not survive a zip, an email, or a
# copy off a filesystem that does not carry POSIX modes -- and losing it here
# would turn "double-click to build" into "Permission denied".
bash ./build_app.sh --install
status=$?

echo
if [[ $status -eq 0 ]]; then
  echo "Done. Just Piano is in /Applications and the 🎹 is in your menu bar."
else
  echo "Build failed (exit $status). The reason is above."
fi
echo "Press Return to close this window."
read -r _
