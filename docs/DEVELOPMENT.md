# Maintainer guide

## Build

Launch/build scripts validate Python 3.11 and preserve incompatible local environments as `.venv.previous-*`. On macOS, a missing interpreter is downloaded privately through uv; explicit `JUSTPIANO_VENV` environments are never replaced. Windows requires the Python 3.11 x64 launcher with Tcl/Tk. Dependencies use `constraints-build.txt`. Keep environments outside source distributions.

| Target | Command | Output |
|---|---|---|
| macOS | `./build_app.sh --dmg` | Architecture-specific DMG in `dist/` |
| Windows | `./build_windows.ps1` | Windows x64 ZIP in `dist/` |

`./run.sh` and `run.bat` run from source. `JUSTPIANO_HOME` isolates settings/cache/recordings. `JUSTPIANO_VENV` selects an existing Mac build environment; `PYTHON` selects the interpreter when creating one.

## GitHub builds and releases

Every branch push, pull request and manual run executes `.github/workflows/release.yml` on `macos-15` (Apple silicon), `macos-15-intel` and `windows-2022`. Build artifacts last 14 days; only the publish job gets `contents: write`.

`python -m tools.release_metadata plan` decides whether a run publishes. A version is published once, either when `justpiano/__init__.py` is bumped on `main` (and the tag is new) or when the matching `v<version>` tag is pushed. Published releases are immutable: bump the version to ship again; a failed upload can be retried through the draft left for that tag.

Each platform package carries a `build-<target>.json` file with its commit, version and SHA-256; the publish job verifies all three before assembling the source ZIP, downloads every upload back to re-verify it, and only then publishes. See `.github/RELEASES.md` for the full trigger and safeguard table.

## Verification

```sh
python -m unittest discover -s tests -v
python -m tools.selftest
python -m tools.tray_smoke
```

On Windows, run `python -m tools.windows_smoke` for real Tk controls with offline audio. Native Windows builds/UI, Intel runtime, Big Sur and Windows 10 device testing remain unverified in this checkout. Newer macOS tests and dependency-wheel availability do not establish minimum-OS compatibility.

The macOS packaging gate checks each Mach-O deployment target. Older local bundles made with Command Line Tools Python require macOS 14; do not distribute them as Big Sur builds. Use a compatible Python distribution and verify the final bundle on the intended OS.

## Data and attribution

Settings/cache: `~/Library/Application Support/Just Piano` on Mac, `%LOCALAPPDATA%/Just Piano` on Windows. Exports default to `~/Music/Just Piano`. Recordings are memory-only until exported. Preserve sample manifests and README audio attribution in every distribution.

## Screenshots and auditions

`python -m tools.capture_readme` captures actual AppKit components with staged states on macOS. No personal settings or hardware are used. Do not label these as live-session or Windows screenshots.

`python -m tools.demo_render demo.wav all` creates auditions for all five sounds. Generated WAV/MIDI files do not belong in source distributions.

## Packaged acceptance gate

Both build scripts execute the frozen application before packaging. It must import native dependencies, render a note, export MIDI/WAV, and create its platform UI. The macOS check also opens/closes the keyboard and dispatches native key handlers. The DMG should additionally be mounted read-only and its enclosed app checked. Native visual inspection and minimum-OS hardware tests remain separate requirements.

Installation refuses to terminate a running piano, preserving memory-only takes. The previous installed app is backed up and replacement is staged before the final move.
