#!/usr/bin/env bash
#
# Double-click this file in Finder to commit everything in this folder and push
# it to GitHub.
#
# Finder runs a .command in a fresh Terminal window with the *home* directory as
# the working directory, not the folder the file is in -- hence the cd below,
# which uses the script's own path so this keeps working wherever the folder is
# moved to.
#
# It shows what it is about to do and waits for a yes before touching anything:
# `git add -A` stages untracked files too, and a blind one is how a stray
# 200 MB render or somebody's private notes end up in a public repository. The
# window is held open at the end so a failed push does not vanish behind a
# Terminal set to close on a clean exit.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

finish() {
  echo
  echo "Press Return to close this window."
  read -r _
  exit "${1:-0}"
}

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "This folder is not a git repository: $(pwd)"
  finish 1
}

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" == "HEAD" ]]; then
  # Detached HEAD has no branch to push to, and guessing one is how work gets
  # pushed somewhere nobody looks. Say so instead.
  echo "You are on a detached HEAD, so there is no branch to push."
  echo "Run:  git switch -c some-branch-name    then try again."
  finish 1
fi

echo "Repository : $(pwd)"
echo "Branch     : $BRANCH"
echo

# ------------------------------------------------------------------ stale locks
# git takes a `.lock` sibling of whatever it is about to write (index.lock,
# HEAD.lock, refs/heads/<branch>.lock) and removes it on the way out. One left
# behind means the command that took it died -- crashed, was interrupted, or ran
# somewhere that could not delete it -- and every later commit then fails with
# "Unable to create ... index.lock: File exists", which reads like a bug in this
# script and is not one.
#
# Removing a lock that a *live* git is holding would corrupt what that git is
# writing, so this never does it silently: it names the files, says how old they
# are, and asks.
# A newline-delimited string and a `while read` loop rather than an array:
# macOS ships bash 3.2, which has no `mapfile`, and where expanding an empty
# array under `set -u` is itself a fatal error. (`build_app.sh` and `run.sh`
# avoid arrays for the same reason.)
LOCKS="$(find "$(git rev-parse --git-dir)" -name '*.lock' -type f 2>/dev/null)"
if [[ -n "$LOCKS" ]]; then
  echo "Stale git lock file(s) are in the way:"
  printf '%s\n' "$LOCKS" | while IFS= read -r lock; do
    # BSD stat, i.e. the one macOS ships. Checked for a plausible date rather
    # than trusting the exit status: GNU stat takes -f to mean "describe the
    # filesystem", so on Linux it *succeeds* and prints a row of numbers. The
    # timestamp is a nicety -- the file itself is the thing worth naming -- so
    # anything unrecognisable degrades to a note instead of to noise.
    age="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$lock" 2>/dev/null)"
    case "$age" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-*) ;;
      *) age="time unknown" ;;
    esac
    echo "  $lock   (last written $age)"
  done
  echo
  echo "These are left behind by a git command that crashed or was interrupted."
  echo "They are also what a git command running RIGHT NOW looks like, so make"
  echo "sure nothing else is mid-commit before saying yes."
  printf 'Remove them and carry on? [y/N] '
  read -r REPLY
  case "$REPLY" in
    [yY]*)
      # One `rm` per line, so a path containing a space is one file and not two.
      FAILED=0
      printf '%s\n' "$LOCKS" | while IFS= read -r lock; do
        rm -f "$lock" || exit 1
      done || FAILED=1
      if [[ $FAILED -ne 0 ]]; then
        echo "Could not remove them. Remove them by hand, then run this again."
        finish 1
      fi
      echo "Removed."
      ;;
    *)
      echo "Left alone — nothing was committed or pushed."
      finish 1
      ;;
  esac
  echo
fi

# --------------------------------------------------------------- what changed
# --porcelain is the stable, script-readable form; the human-readable default
# has changed wording between git versions.
CHANGES="$(git status --porcelain)"
UNPUSHED=0
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  UNPUSHED="$(git rev-list --count '@{u}..HEAD')"
else
  # No upstream yet: every commit on this branch is unpushed by definition.
  UNPUSHED="$(git rev-list --count HEAD --not --remotes)"
fi

if [[ -z "$CHANGES" && "$UNPUSHED" -eq 0 ]]; then
  echo "Nothing to commit and nothing to push — already up to date."
  finish 0
fi

if [[ -n "$CHANGES" ]]; then
  echo "These changes will be committed:"
  echo
  git status --short
  echo
  # Untracked files are the ones worth a second look, so they get named again
  # rather than being left as a '??' in the list above.
  NEW="$(git ls-files --others --exclude-standard)"
  if [[ -n "$NEW" ]]; then
    echo "New files not previously tracked by git:"
    # Indented with sed rather than `printf '  %s\n' $NEW`, which splits on
    # spaces: "Build Just Piano.command" came out as three separate files.
    printf '%s\n' "$NEW" | sed 's/^/  /'
    echo
  fi
fi
if [[ "$UNPUSHED" -gt 0 ]]; then
  echo "Already committed, waiting to be pushed:"
  git --no-pager log --oneline '@{u}..HEAD' 2>/dev/null \
    || git --no-pager log --oneline HEAD --not --remotes
  echo
fi

# ------------------------------------------------------------------- go ahead
if [[ -n "$CHANGES" ]]; then
  printf 'Commit message (Return on its own to cancel): '
  read -r MESSAGE
  if [[ -z "${MESSAGE// }" ]]; then
    echo "Cancelled — nothing was committed or pushed."
    finish 1
  fi
  git add -A || finish 1
  git commit -m "$MESSAGE" || finish 1
  echo
else
  printf 'Push %s commit(s) to origin/%s? [y/N] ' "$UNPUSHED" "$BRANCH"
  read -r REPLY
  case "$REPLY" in
    [yY]*) ;;
    *) echo "Cancelled — nothing was pushed."; finish 1 ;;
  esac
fi

# ---------------------------------------------------------------------- push
# -u on every push, not only the first: it is a no-op once the upstream is set,
# and it is what makes a brand-new branch land somewhere `git status` can see.
echo "==> Pushing $BRANCH to origin"
if git push -u origin "$BRANCH"; then
  echo
  echo "Done — origin/$BRANCH is up to date."
  finish 0
fi

echo
echo "The push failed. The reason is above; the usual ones are:"
echo "  * no network, or GitHub is unreachable"
echo "  * your SSH key is not loaded — try:  ssh -T git@github.com"
echo "  * the branch moved on GitHub — try:  git pull --rebase, then run this again"
echo
echo "Your commit is safe locally either way; nothing has been lost."
finish 1
