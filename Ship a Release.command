#!/usr/bin/env bash
#
# Double-click this file in Finder to publish a new release of Just Piano.
#
# It does not build anything here. It checks that the repository is in a fit
# state to release, then pushes a version tag; GitHub Actions picks the tag up,
# builds the .app on a real Mac, and attaches the disk image to a release. That
# split is deliberate -- a release built on whatever happens to be installed on
# one laptop is not reproducible, and "works on mine" is how a broken build gets
# shipped.
#
# Use "Commit and Push.command" for ordinary work. This is only for releases.
#
# Finder runs a .command in a fresh Terminal window with the *home* directory as
# the working directory, not the folder the file is in -- hence the cd below,
# which uses the script's own path so this keeps working wherever the folder is
# moved to.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

finish() {
  echo
  echo "Press Return to close this window."
  read -r _
  exit "${1:-0}"
}

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '  \033[31mno\033[0m   %s\n' "$*"; }
pass() { printf '  \033[32mok\033[0m   %s\n' "$*"; }

git rev-parse --git-dir >/dev/null 2>&1 || {
  echo "This folder is not a git repository: $(pwd)"
  finish 1
}

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' justpiano/__init__.py)"
if [[ -z "$VERSION" ]]; then
  echo "No __version__ found in justpiano/__init__.py — nothing to release."
  finish 1
fi
TAG="v$VERSION"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "Repository : $(pwd)"
echo "Branch     : $BRANCH"
echo "Version    : $VERSION  (tag $TAG)"

# ----------------------------------------------------------------- the checks
# Everything that would produce a bad release is checked before anything is
# pushed, and all of it is reported at once: finding four problems one Terminal
# window at a time is four rounds of this script.
say "Checking"
READY=1

if [[ -n "$(git status --porcelain)" ]]; then
  fail "there are uncommitted changes — run \"Commit and Push.command\" first"
  READY=0
else
  pass "working tree is clean"
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  fail "tag $TAG already exists — bump __version__ in justpiano/__init__.py"
  READY=0
else
  pass "tag $TAG is free"
fi

if git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1; then
  fail "tag $TAG is already on GitHub — bump __version__ and try again"
  READY=0
else
  pass "tag $TAG is free on GitHub too"
fi

UNPUSHED="$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo 0)"
if [[ "$UNPUSHED" -gt 0 ]]; then
  fail "$UNPUSHED commit(s) not pushed — the build would not include them"
  READY=0
else
  pass "origin/$BRANCH is up to date"
fi

MISSING=""
for pack in salamander uprightkw; do
  [[ -f "assets/samples/$pack/pack.json" ]] || MISSING="$MISSING $pack"
done
if [[ -n "$MISSING" ]]; then
  fail "sample pack(s) missing:$MISSING — the app would fall back to synthesis"
  READY=0
else
  pass "sample packs are present ($(find assets/samples -name '*.flac' | wc -l | tr -d ' ') recordings)"
fi

if [[ ! -f .github/workflows/release.yml ]]; then
  fail "no .github/workflows/release.yml — nothing would build the app"
  READY=0
else
  pass "the release workflow is in place"
fi

if [[ $READY -eq 0 ]]; then
  echo
  echo "Nothing was pushed. Fix the above and run this again."
  finish 1
fi

# ------------------------------------------------------------ the self-test
# Offered rather than forced: it takes a couple of minutes, and the workflow
# runs it again on GitHub before it will publish anything. Saying no here only
# means finding out later instead of now.
if [[ -x .venv/bin/python ]]; then
  say "Self-test"
  printf 'Run the self-test before tagging? (a few minutes) [Y/n] '
  read -r REPLY
  case "$REPLY" in
    [nN]*) echo "  skipped — GitHub will run it before publishing" ;;
    *)
      if PYTHONPATH="$PWD" .venv/bin/python tools/selftest.py; then
        pass "self-test passed"
      else
        echo
        fail "self-test failed — nothing was tagged"
        finish 1
      fi ;;
  esac
fi

# ------------------------------------------------------------------- go ahead
say "Ready to release $TAG"
cat <<EOF
  This will push the tag $TAG to GitHub, which starts a build that:
    * runs the self-test on a macOS runner
    * builds Just Piano.app and packages JustPiano-$VERSION.dmg
    * publishes a release with the disk image attached

  Nothing is uploaded from this machine.
EOF
echo
printf "Push tag %s? [y/N] " "$TAG"
read -r REPLY
case "$REPLY" in
  [yY]*) ;;
  *) echo "Cancelled — nothing was tagged or pushed."; finish 1 ;;
esac

git tag -a "$TAG" -m "Just Piano $VERSION" || finish 1
if ! git push origin "$TAG"; then
  echo
  echo "The push failed, so the tag exists only on this machine."
  echo "Remove it with:  git tag -d $TAG"
  echo "The usual causes are no network, or an SSH key that is not loaded"
  echo "(check with:  ssh -T git@github.com)."
  finish 1
fi

REMOTE="$(git remote get-url origin 2>/dev/null)"
SLUG="$(printf '%s' "$REMOTE" | sed -e 's#^git@github.com:##' -e 's#^https://github.com/##' -e 's#\.git$##')"
say "Tagged and pushed"
echo "  The build is starting now. Watch it at:"
echo "    https://github.com/$SLUG/actions"
echo
echo "  When it finishes, the release will be at:"
echo "    https://github.com/$SLUG/releases/tag/$TAG"
echo
echo "  To release again, bump __version__ in justpiano/__init__.py, commit,"
echo "  and run this file once more."
finish 0
