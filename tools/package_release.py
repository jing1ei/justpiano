"""Create stable installer names, a tracked-source ZIP and SHA-256 checksums."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

from justpiano import __version__

ASSETS = {
    f"JustPiano-{__version__}-macOS-arm64.dmg": "JustPiano-1.0.0-macOS-arm64.dmg",
    f"JustPiano-{__version__}-macOS-x64.dmg": "JustPiano-1.0.0-macOS-x64.dmg",
    f"JustPiano-{__version__}-Windows-x64.zip": "JustPiano-1.0.0-Windows-x64.zip",
}


def source_archive(destination: Path, revision: str = "HEAD") -> Path:
    root = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "archive", "--format=zip", "--prefix=JustPiano/",
                    "-o", str(destination), revision], cwd=root, check=True)
    return destination


def package(directory: Path) -> Path:
    missing = [name for name in ASSETS if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing build artifacts: " + ", ".join(missing))
    output = directory / "downloads"
    if output.exists():
        raise FileExistsError(f"Use a fresh output directory: {output}")
    output.mkdir()
    for source, name in ASSETS.items():
        shutil.copy2(directory / source, output / name)
    source_archive(output / "JustPiano-1.0.0-source.zip")
    sums = []
    for path in sorted(output.iterdir()):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        sums.append(f"{digest.hexdigest()}  {path.name}")
    (output / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    print(package(Path(sys.argv[1])))
