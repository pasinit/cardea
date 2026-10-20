"""Fetch the pinned OpenCode binary for this platform and verify it (ADR-033).

Usage: python3 packaging/fetch_opencode.py DEST_DIR

Downloads the platform package of `opencode-ai` from the npm registry, checks the archive's
SHA-256 against the pins below, extracts `bin/opencode` into DEST_DIR and prints the
binary's path and SHA-256 (the value a harness registry entry pins). Used by CI for the
end-to-end harness tests and by host owners installing the harness.
"""

import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.18.35"  # released 2026-10-06
# SHA-256 of each npm platform archive at VERSION; their npm sha512 integrity matches.
ARCHIVES = {
    ("Linux", "x86_64"): (
        "opencode-linux-x64",
        "e5c8997d5d0024d9686a849f20d462766d83646e74d9751dfb172715f2216133",
    ),
    ("Linux", "aarch64"): (
        "opencode-linux-arm64",
        "57fd6ecfea7927c79d3943766db39ef9267f38bdb32babe6b3d8dea1f21484ba",
    ),
    ("Darwin", "arm64"): (
        "opencode-darwin-arm64",
        "b626543f4427cbd7a59756c24045f6f32dbc4cf7347ab5a8613f5c9fd6b0eeb3",
    ),
    ("Darwin", "x86_64"): (
        "opencode-darwin-x64",
        "ba2d7b12fcb16f687e3b5208aa67a882d17065f4cd3f00df60fdc4fb1223ffdb",
    ),
}
REGISTRY = "https://registry.npmjs.org"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    key = (platform.system(), platform.machine())
    if key not in ARCHIVES:
        print(f"no pinned OpenCode archive for {key}", file=sys.stderr)
        return 1
    package, expected = ARCHIVES[key]
    url = f"{REGISTRY}/{package}/-/{package}-{VERSION}.tgz"
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
        archive = response.read()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected:
        print(f"{package}-{VERSION}.tgz has SHA-256 {digest}, expected {expected}", file=sys.stderr)
        return 1
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.extractfile("package/bin/opencode")
        if member is None:
            print("archive has no package/bin/opencode", file=sys.stderr)
            return 1
        binary = member.read()
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "opencode"
    path.write_bytes(binary)
    path.chmod(0o755)
    print(path.resolve())
    print(hashlib.sha256(binary).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
