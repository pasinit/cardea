"""Stage explicit package inputs, excluding credentials, caches and developer artifacts."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def stage(destination: Path, uv: str) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        ROOT / "apps/api/src",
        destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(ROOT / "packaging/debian", destination / "debian")
    shutil.copyfile(ROOT / "apps/api/uv.lock", destination / "uv.lock")
    with (destination / "requirements.txt").open("w") as requirements:
        subprocess.run(
            [
                uv,
                "export",
                "--locked",
                "--project",
                str(ROOT / "apps/api"),
                "--no-dev",
                "--no-emit-project",
                "--no-header",
            ],
            stdout=requirements,
            check=True,
        )
    manifest = {
        str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    (destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for name in ("rules", "cardea", "cardea-kernel.postinst", "cardea-kernel.prerm"):
        (destination / "debian" / name).chmod(0o755)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    stage(args.destination.resolve(), args.uv)
