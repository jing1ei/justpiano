#!/usr/bin/env bash
#
# Double-click this file ONCE, in Finder, to put the repository into a clean
# state for publishing. Then delete it — it offers to delete itself at the end.
#
# It does three things, and the middle one cannot be undone:
#
#   1. removes leftover scratch files and a stale git lock;
#   2. replaces the whole commit history with a single initial commit;
#   3. renames the branch to `main` and repacks the repository.
#
# Step 2 is what "cut dead history" means: the old commits, their messages and
# every file version they held stop existing locally. There is no way back
# except a copy of the folder, so this makes one first and tells you where it
# put it.
#
# If the repository is already on GitHub with the old history, the next push
# has to be `git push --force-with-lease`, and anyone who cloned it will need
# to re-clone. That is the price of rewriting history and it is worth being
# sure about before saying yes.
#
# Finder runs a .command in a fresh Terminal window with the *home* directory as
# the working directory, not the folder the file is in -- hence the cd below.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

finish() {
  echo
  echo "Press Return to close this window."
  read -r _
  exit "${1:-0}"
}
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "This folder is not a git repository: $(pwd)"; finish 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' justpiano/__init__.py)"
COMMITS="$(git rev-list --count HEAD 2>/dev/null || echo 0)"

echo "Repository : $(pwd)"
echo "Branch     : $BRANCH"
echo "History    : $COMMITS commit(s)"
echo "Version    : ${VERSION:-unknown}"

# ------------------------------------------------------------------- scratch
# Named individually rather than matched by a pattern: a glob that deletes
# things in someone's repository is exactly the kind of convenience that
# eventually eats the wrong file.
SCRATCH="
.deltest
.permcheck
assets/salamander-pack.tar
assets/upright-pack.tar
"
say "Leftovers to remove"
FOUND=0
printf '%s\n' "$SCRATCH" | while IFS= read -r f; do
  [ -n "$f" ] && [ -e "$f" ] && note "$f  ($(wc -c <"$f" | tr -d ' ') bytes)"
done
for f in $SCRATCH; do [ -e "$f" ] && FOUND=1; done
LOCKS="$(find "$(git rev-parse --git-dir)" -name '*.lock' -type f 2>/dev/null)"
[ -n "$LOCKS" ] && printf '%s\n' "$LOCKS" | while IFS= read -r l; do note "$l"; done
[ "$FOUND" -eq 0 ] && [ -z "$LOCKS" ] && note "(none)"

say "What this will do"
cat <<EOF
  * back the folder up next to itself, then
  * delete the leftovers above,
  * replace $COMMITS commit(s) with one "Just Piano ${VERSION:-1.0.0}" commit,
  * rename the branch to main, and repack.

  Everything currently in the folder is kept — this rewrites history, not files.
EOF
echo
printf 'Go ahead? [y/N] '
read -r REPLY
case "$REPLY" in [yY]*) ;; *) echo "Cancelled — nothing was changed."; finish 1 ;; esac

# -------------------------------------------------------------------- backup
BACKUP="../$(basename "$(pwd)")-before-cleanup-$(date +%Y%m%d-%H%M%S)"
say "Backing up to $BACKUP"
if cp -R "$(pwd)" "$BACKUP"; then
  note "done — delete it once you are happy with the result"
else
  echo "  Backup failed. Nothing has been changed."
  finish 1
fi

# ------------------------------------------------------------------- cleanup
say "Removing leftovers"
for f in $SCRATCH; do
  [ -e "$f" ] && { rm -f "$f" && note "removed $f" || note "could not remove $f"; }
done
if [ -n "$LOCKS" ]; then
  printf '%s\n' "$LOCKS" | while IFS= read -r l; do
    rm -f "$l" && note "removed $l"
  done
fi
find "$(git rev-parse --git-dir)/objects" -name 'tmp_obj_*' -delete 2>/dev/null

# ------------------------------------------------------------------- history
say "Rewriting history"
git checkout --orphan _clean >/dev/null 2>&1 || { echo "  failed"; finish 1; }
git add -A || finish 1
# This script is a one-time tool, not part of the project. Staged by `add -A`
# like everything else, then dropped from the index so the clean commit does not
# carry a file whose whole purpose is to have already run.
git rm -q --cached "$(basename "$SELF")" >/dev/null 2>&1 || true
git commit -q -m "Just Piano ${VERSION:-1.0.0}" || { echo "  nothing to commit"; finish 1; }
note "one commit created ($(git ls-files | wc -l | tr -d ' ') files tracked)"

for b in $(git for-each-ref --format='%(refname:short)' refs/heads/); do
  [ "$b" = "_clean" ] || git branch -q -D "$b" 2>/dev/null
done
git branch -q -m main
note "branch is now main"

git reflog expire --expire=now --all >/dev/null 2>&1
git gc --prune=now --quiet >/dev/null 2>&1
note "repacked: $(git count-objects -vH | sed -n 's/^size-pack: //p')"

say "Done"
echo "  History : $(git rev-list --count HEAD) commit — $(git --no-pager log --oneline)"
echo "  Backup  : $BACKUP"
echo
REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [ -n "$REMOTE" ]; then
  cat <<EOF
  This repository still points at:
    $REMOTE

  The old history is still on GitHub, so the next push has to overwrite it:

    git push --force-with-lease origin main

  Do that once, set the default branch to main in the repository settings on
  GitHub, and delete the old branch there. After that, "Commit and Push.command"
  and "Ship a Release.command" work normally.
EOF
else
  echo "  No remote is set. Add one with:  git remote add origin <url>"
fi

echo
printf 'Delete this cleanup script now that it has run? [Y/n] '
read -r REPLY
case "$REPLY" in
  [nN]*) echo "  Left in place — remember it rewrites history if run again." ;;
  *) rm -f "$SELF" && echo "  Deleted. (It is committed in the backup if you want it back.)" ;;
esac
finish 0
