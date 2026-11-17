"""Refuse to build unless the staged tree is exactly what `packaging/stage.py` recorded.

`source-manifest.json` lists every staged file with its SHA-256. A copy to the builder can
add files the manifest never saw. macOS `tar`, `cp` and `scp` write an AppleDouble `._NAME`
file next to each file that has extended attributes. Under `src/`, `dh_install` ships those
files in the package, and Alembic then tries to load `versions/._m1_001.py` as a migration:
`cardea init` fails with "source code string cannot contain null bytes". The build must
see the tree the manifest describes and nothing else.

Files that debhelper and dpkg-buildpackage create inside `debian/` are allowed.
"""

import hashlib
import json
import sys
from pathlib import Path

MANIFEST = "source-manifest.json"
# What `debian/rules clean`/`build` and dpkg-buildpackage write into the tree themselves.
GENERATED_DIRS = ("debian/.debhelper/", "debian/cardea-kernel/", "debian/tmp/")
GENERATED_FILES = ("debian/files", "debian/debhelper-build-stamp")
GENERATED_SUFFIXES = (".substvars", ".debhelper.log", ".debhelper")


def generated(name: str) -> bool:
    return (
        name in GENERATED_FILES
        or name.startswith(GENERATED_DIRS)
        or (name.startswith("debian/") and name.endswith(GENERATED_SUFFIXES))
    )


def problems(root: Path) -> list[str]:
    """Every difference between the tree under `root` and its manifest, as text."""
    manifest = json.loads((root / MANIFEST).read_text())
    found = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink()) and path.name != MANIFEST
    }
    found = {name for name in found if not generated(name)}
    errors = [f"not in the manifest: {name}" for name in sorted(found - set(manifest))]
    errors += [f"missing: {name}" for name in sorted(set(manifest) - found)]
    for name in sorted(found & set(manifest)):
        path = root / name
        if path.is_symlink():
            errors.append(f"a symbolic link: {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != manifest[name]:
            errors.append(f"changed: {name}")
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    errors = problems(root)
    if not errors:
        return 0
    for line in errors[:50]:
        print(f"verify_stage: {line}", file=sys.stderr)
    if len(errors) > 50:
        print(f"verify_stage: ... and {len(errors) - 50} more", file=sys.stderr)
    if any("/._" in line or ": ._" in line for line in errors):
        print(
            "verify_stage: AppleDouble ._ files: copy the stage without macOS metadata, "
            "for example COPYFILE_DISABLE=1 tar, or stage it again on the builder",
            file=sys.stderr,
        )
    print("verify_stage: the staged tree does not match source-manifest.json", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
