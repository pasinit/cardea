"""Fetch the pinned Gitleaks binary for this platform and verify it (ADR-037 decision 4).

Usage: python3 packaging/fetch_gitleaks.py DEST_DIR

Downloads the platform archive of the Gitleaks release below from GitHub, checks the
archive's SHA-256 against the pins below, extracts `gitleaks` into DEST_DIR and prints the
binary's path and SHA-256. The SHA-256 is the value the content gate's configuration pins
as `gitleaks_sha256`. CI uses it for the content-gate tests, and host owners use it to
install the gate.
"""

import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "8.30.1"  # released 2026-03-21; MIT
# SHA-256 of each release archive, as in the release's gitleaks_<version>_checksums.txt.
ARCHIVES = {
    ("Linux", "x86_64"): (
        "linux_x64",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
    ("Linux", "aarch64"): (
        "linux_arm64",
        "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    ),
    ("Darwin", "arm64"): (
        "darwin_arm64",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Darwin", "x86_64"): (
        "darwin_x64",
        "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    ),
}
RELEASES = "https://github.com/gitleaks/gitleaks/releases/download"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    key = (platform.system(), platform.machine())
    if key not in ARCHIVES:
        print(f"no pinned Gitleaks archive for {key}", file=sys.stderr)
        return 1
    suffix, expected = ARCHIVES[key]
    name = f"gitleaks_{VERSION}_{suffix}.tar.gz"
    url = f"{RELEASES}/v{VERSION}/{name}"
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
        archive = response.read()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != expected:
        print(f"{name} has SHA-256 {digest}, expected {expected}", file=sys.stderr)
        return 1
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        member = bundle.extractfile("gitleaks")
        if member is None:
            print("archive has no gitleaks binary", file=sys.stderr)
            return 1
        binary = member.read()
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "gitleaks"
    path.write_bytes(binary)
    path.chmod(0o755)
    print(path.resolve())
    print(hashlib.sha256(binary).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
