# Package contract

Public installer names follow `JustPiano-<version>-<system>-<architecture>.<extension>`.

- `macOS-arm64.dmg` contains only Apple silicon binaries.
- `macOS-x64.dmg` contains only Intel binaries.
- `Windows-x64.exe` is an installer.

The .app is inside the DMG. App ZIPs and portable Windows ZIPs are internal build inputs,
and are not attached as additional public installers. Each version also carries one
`JustPiano-<version>-source.zip` and one `SHA256SUMS.txt`.

Every required platform must pass its existing tests and packaging checks before publication.
An incomplete release remains a draft. Published versions and their tags are immutable.
Bump the patch version to publish changes to packaging.
