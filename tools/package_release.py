"""Create stable installer names, a tracked-source ZIP and SHA-256 checksums."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from justpiano import __version__
from tools.release_metadata import TARGETS, digest, package_name

ASSETS = [package_name(target) for target in TARGETS]
SOURCE = f"JustPiano-{__version__}-source.zip"
RELEASE_FILES = [*ASSETS, SOURCE]


def source_archive(destination: Path, revision: str = "HEAD") -> Path:
    root = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "archive", "--format=zip", f"--prefix=JustPiano-{__version__}/",
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
    for name in ASSETS:
        shutil.copy2(directory / name, output / name)
    source_archive(output / SOURCE)
    sums = [f"{digest(output / name)}  {name}" for name in sorted(RELEASE_FILES)]
    (output / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    print(package(Path(sys.argv[1])))
