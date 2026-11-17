"""Build and pin the `env.try` toolchain image (ADR-038 decision 6).

Usage, as root on the Debian host (`make try-toolchain`):

    python3 packaging/try_toolchain.py build [--root /opt/cardea-try/toolchain] [--keep 2]
    python3 packaging/try_toolchain.py pin REGISTRY [--root ...] [--sha256 DIGEST]

`build` runs `mmdebstrap` (Debian's reproducible bootstrapper) for a minimal Debian 13
root with the packages the scenarios need, writes it as a tarball, takes its SHA-256 and
unpacks it read-only at `<root>/<sha256>/`. It prints the digest. Older images beyond
`--keep` are removed, never the one a registry pins. `SOURCE_DATE_EPOCH` is set to the
start of the current ISO week, so a rebuild in the same week of the same package set gives
the same tarball.

`pin` writes the image's path and digest into every `environment.toolchain` of the
repository registry, atomically, so the next `env.try` call has a new run key. The host
owner refreshes weekly, for example with the kit's `cardea-try-toolchain.timer`.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

SUITE = "trixie"
PACKAGES = (
    "ca-certificates",
    "git",
    "make",
    "python3",
    "python3-venv",
    "curl",
    "xz-utils",
)
DEFAULT_ROOT = Path("/opt/cardea-try/toolchain")
WEEK = 7 * 24 * 3600
# The ISO week starts on Monday; the Unix epoch was a Thursday.
MONDAY_OFFSET = 3 * 24 * 3600


def week_start(now: float) -> int:
    return int((now + MONDAY_OFFSET) // WEEK * WEEK - MONDAY_OFFSET)


def mmdebstrap_argv(target: Path) -> list[str]:
    return [
        "/usr/bin/mmdebstrap",
        "--variant=minbase",
        "--mode=root",
        "--format=tar",
        f"--include={','.join(PACKAGES)}",
        SUITE,
        str(target),
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unpack(tarball: Path, root: Path, digest: str) -> Path:
    target = root / digest
    if target.exists():
        return target
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".unpack-"))
    with tarfile.open(tarball) as archive:
        archive.extractall(staging, filter="tar")
    for path in [staging, *staging.rglob("*")]:
        if not path.is_symlink():
            mode = path.lstat().st_mode
            path.chmod(mode & ~0o222)
    staging.rename(target)
    return target


def pinned(registry: Path) -> set[str]:
    document = json.loads(registry.read_text()) if registry.exists() else {}
    found = set()
    for entry in document.get("repositories", {}).values():
        toolchain = (entry.get("environment") or {}).get("toolchain")
        if isinstance(toolchain, dict) and isinstance(toolchain.get("sha256"), str):
            found.add(toolchain["sha256"])
    return found


def prune(root: Path, keep: int, current: str, protected: set[str]) -> list[str]:
    images = sorted(
        (p for p in root.iterdir() if p.is_dir() and len(p.name) == 64),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for image in images[keep:]:
        if image.name in protected or image.name == current:
            continue
        for path in [image, *image.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.lstat().st_mode | 0o200)
        shutil.rmtree(image)
        removed.append(image.name)
    return removed


def build(root: Path, keep: int, registry: Path | None) -> str:
    if os.geteuid() != 0:
        raise SystemExit("build the toolchain as root")
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(week_start(time.time()))}
    with tempfile.TemporaryDirectory(dir=root, prefix=".build-") as scratch:
        tarball = Path(scratch) / "toolchain.tar"
        subprocess.run(mmdebstrap_argv(tarball), env=env, check=True)
        digest = sha256_file(tarball)
        unpack(tarball, root, digest)
    prune(root, keep, digest, pinned(registry) if registry else set())
    return digest


def pin(registry: Path, root: Path, digest: str) -> dict[str, Any]:
    """Point every repository's environment at this image; atomic replace."""
    image = root / digest
    if not image.is_dir():
        raise SystemExit(f"no unpacked image at {image}")
    document = json.loads(registry.read_text())
    for entry in document.get("repositories", {}).values():
        environment = entry.get("environment")
        if isinstance(environment, dict):
            environment["toolchain"] = {"path": str(image), "sha256": digest}
    staged = registry.with_name(f".{registry.name}.pin")
    staged.write_text(json.dumps(document, indent=2) + "\n")
    staged.chmod(registry.stat().st_mode & 0o777)
    os.replace(staged, registry)
    result: dict[str, Any] = document
    return result


def latest(root: Path) -> str:
    images = [p for p in root.iterdir() if p.is_dir() and len(p.name) == 64]
    if not images:
        raise SystemExit("no toolchain image built yet")
    return max(images, key=lambda p: p.stat().st_mtime).name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    building = commands.add_parser("build")
    building.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    building.add_argument("--keep", type=int, default=2)
    building.add_argument("--registry", type=Path, default=None)
    pinning = commands.add_parser("pin")
    pinning.add_argument("registry", type=Path)
    pinning.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    pinning.add_argument("--sha256", default=None)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build(args.root, args.keep, args.registry))
        return 0
    pin(args.registry, args.root, args.sha256 or latest(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
