"""Build and pin the `env.try` toolchain image (ADR-038 decision 6).

Usage, as root on the Debian host (`make try-toolchain`):

    python3 packaging/try_toolchain.py build [--root /opt/cardea-try/toolchain] [--keep 2]
    python3 packaging/try_toolchain.py pin REGISTRY [--root ...] [--sha256 DIGEST]

`build` runs `mmdebstrap` (Debian's reproducible bootstrapper) for a minimal Debian 13
root with the packages the scenarios need, plus the pinned `uv` release binary at
`/usr/local/bin/uv` (Debian 13 does not package uv; the archive is downloaded from the
release, checked against the SHA-256 pinned below before anything is built, and copied in
with a customize hook). It writes the root as a tarball, takes its SHA-256 and
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
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable
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
# uv for repositories whose scenarios run through it (Cardea's own `make try`, `make teams`).
UV_VERSION = "0.13.7"  # released 2026-11-03; Apache-2.0 OR MIT
# SHA-256 of each release archive, as in the release's `<archive>.sha256` files.
UV_ARCHIVES = {
    "x86_64": (
        "x86_64-unknown-linux-gnu",
        "c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83",
    ),
    "aarch64": (
        "aarch64-unknown-linux-gnu",
        "71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc",
    ),
}
UV_RELEASES = "https://github.com/astral-sh/uv/releases/download"
UV_PATH = "/usr/local/bin/uv"
DEFAULT_ROOT = Path("/opt/cardea-try/toolchain")
WEEK = 7 * 24 * 3600
# The ISO week starts on Monday; the Unix epoch was a Thursday.
MONDAY_OFFSET = 3 * 24 * 3600


def week_start(now: float) -> int:
    return int((now + MONDAY_OFFSET) // WEEK * WEEK - MONDAY_OFFSET)


def uv_archive(machine: str) -> tuple[str, str, str]:
    """The release archive's URL, its member path for the `uv` binary, and its pinned SHA-256."""
    if machine not in UV_ARCHIVES:
        raise SystemExit(f"no pinned uv archive for {machine}")
    triple, expected = UV_ARCHIVES[machine]
    name = f"uv-{triple}.tar.gz"
    return f"{UV_RELEASES}/{UV_VERSION}/{name}", f"uv-{triple}/uv", expected


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310 - fixed https URL
        data: bytes = response.read()
    return data


def fetch_uv(
    destination: Path,
    epoch: int,
    machine: str | None = None,
    download: Callable[[str], bytes] | None = None,
) -> Path:
    """Download the pinned uv archive for the build architecture, verify it, extract `uv`."""
    url, member, expected = uv_archive(machine or platform.machine())
    archive = (download or _download)(url)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected:
        raise SystemExit(f"{url} has SHA-256 {digest}, expected {expected}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        try:
            info = bundle.getmember(member)
        except KeyError:
            raise SystemExit(f"{url} has no {member}") from None
        extracted = bundle.extractfile(info) if info.isfile() else None
        if extracted is None:
            raise SystemExit(f"{member} in {url} is not a regular file")
        binary = extracted.read()
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "uv"
    path.write_bytes(binary)
    path.chmod(0o755)
    # The copy keeps its metadata: date it like the rest of the image.
    os.utime(path, (epoch, epoch))
    return path


def mmdebstrap_argv(target: Path, uv: Path) -> list[str]:
    # mmdebstrap's root mode makes every mount of the namespace it runs in private. Run in
    # the host's namespace, it left `/` private, and systemd then silently stopped giving
    # units their LoadCredential files: every credentialed confined worker (session,
    # coding, vcs) failed until a reboot. A mount namespace of its own keeps the change
    # there (found on the first Debian acceptance run).
    return [
        "/usr/bin/unshare",
        "--mount",
        "--propagation",
        "private",
        "--",
        "/usr/bin/mmdebstrap",
        "--variant=minbase",
        "--mode=root",
        "--format=tar",
        f"--include={','.join(PACKAGES)}",
        # The verified uv binary, keeping its mode and date; then the directory's date, which
        # the copy changed, so the tarball stays the same for the same week; then a check that
        # the binary runs.
        f"--customize-hook=copy-in {uv} {Path(UV_PATH).parent}",
        f'--customize-hook=touch --no-dereference --date="@$SOURCE_DATE_EPOCH" "$1{Path(UV_PATH).parent}"',
        f'--customize-hook="$1{UV_PATH}" --version',
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
    epoch = week_start(time.time())
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
    with tempfile.TemporaryDirectory(dir=root, prefix=".build-") as scratch:
        tarball = Path(scratch) / "toolchain.tar"
        uv = fetch_uv(Path(scratch) / "uv", epoch)
        subprocess.run(mmdebstrap_argv(tarball, uv), env=env, check=True)
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
