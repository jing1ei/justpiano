> The current public package formats and names are defined in [Package contract](PACKAGING.md).

# Automatic releases

[`.github/workflows/release.yml`](workflows/release.yml) builds, tests and
packages JustPiano on three GitHub-hosted runners:

- `macos-15` — Apple silicon (`macOS-arm64` DMG)
- `macos-15-intel` — Intel (`macOS-x64` DMG)
- `windows-2022` — 64-bit Windows (`Windows-x64` EXE installer)

Every branch push, pull request and manual workflow run goes through the full
matrix but only uploads workflow artifacts (retained 14 days). It does not
publish a release.

## When a release is published

`tools/release_metadata.py` plans every run from the version in
[`justpiano/__init__.py`](../justpiano/__init__.py):

| Trigger | Result |
|---|---|
| Pull request | Build and test only |
| Push to a non-`main` branch | Build and test only |
| Push to `main` with a new version whose tag does not exist yet | Publish `v<version>` after all three platform builds pass |
| Push to `main` when `v<version>` already exists | Build only; bump the version to release again |
| Push tag `v<version>` matching the package version | Publish that tag after all three platform builds pass |

A published release is **never replaced or re-tagged**. Shipping another build
means bumping `__version__` (prereleases like `1.0.1rc1` are marked
"prerelease" and are not made "latest"). To republish a failed upload, reuse
the draft left at `v<version>`; the publisher updates an existing draft and
only goes public after verification.

## Required packages

For version `X.Y.Z`:

- `JustPiano-X.Y.Z-macOS-arm64.dmg`
- `JustPiano-X.Y.Z-macOS-x64.dmg`
- `JustPiano-X.Y.Z-Windows-x64.exe`
- `JustPiano-X.Y.Z-source.zip` (tracked files from the built commit)
- `SHA256SUMS.txt`

Each binary package carries a `build-<target>.json` identity file with its
commit, version and SHA-256; `tools.release_metadata.verify` requires all
three to match the publishing commit before the publish job runs.

## Publication safeguards

The `publish` job:

1. requires all three platform builds;
2. verifies every package's commit, version and checksum;
3. assembles the source ZIP from the same commit and writes `SHA256SUMS.txt`;
4. uploads into a **draft** release with `contents: write` (all other jobs use
   read-only permissions);
5. downloads every uploaded asset back and re-hashes it;
6. refuses to publish if the tag already points at another commit or a public
   release for the version already exists;
7. only then marks the release public (prereleases are not marked "latest").

The workflow also mounts each macOS DMG and verifies the app's code signature
and architecture inside it. Ad-hoc signed, non-notarized builds still require
the normal right-click/Open confirmation on other Macs.
