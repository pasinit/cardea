"""The `test-changed` scenario's runner for `env.try`: fast feedback on a commit, running only
the tests for what it changed since its base. The full `check` scenario still gates merges.

Usage (`make test-changed`):

    python3 packaging/test_changed.py [--base REF]

The base is the merge base of HEAD and `origin/main` (or `--base`). The selection is:

- test files the commit changed under `apps/api/tests/`;
- test files that import a changed Python module or name its file (`<stem>.py`), for a
  changed `.py` file anywhere else in the repository.

It runs `make lint` and then pytest on the selection, in parallel and without the tests that
need TCP loopback, as `check` does. With nothing selected it runs lint alone and says so. A
selection over `MAX_FILES` files runs the whole `check-test` suite instead. The last line is
`test-changed: PASS` or `test-changed: FAIL`, and the exit status agrees.
"""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

TESTS = Path("apps/api/tests")
API = Path("apps/api")
MAX_FILES = 40
# Stems too generic to select tests by name.
GENERIC = {"__init__", "__main__", "models", "conftest", "setup", "main", "utils", "config"}


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def base_of(explicit: str | None) -> str:
    if explicit:
        return explicit
    for ref in ("origin/main", "origin/HEAD", "main"):
        found = git("merge-base", "HEAD", ref)
        if found:
            return found
    return "HEAD~1"


def changed(base: str) -> list[str]:
    listed = git("diff", "--name-only", "--diff-filter=ACMR", f"{base}..HEAD")
    return [line for line in listed.splitlines() if line]


def select(paths: list[str], root: Path = Path(".")) -> list[str]:
    """The test files to run for `paths`, relative to `apps/api`."""
    tests = sorted(p for p in (root / TESTS).rglob("test_*.py"))
    chosen: set[Path] = set()
    stems: set[str] = set()
    for path in paths:
        target = Path(path)
        if target.suffix != ".py":
            continue
        if TESTS in target.parents and target.name.startswith("test_"):
            chosen.add(root / target)
        elif target.stem not in GENERIC:
            stems.add(target.stem.removeprefix("test_"))
    if stems:
        names = "|".join(sorted(map(re.escape, stems)))
        # A test refers to a module when it imports it or names its file.
        pattern = re.compile(
            rf"^\s*(?:from|import)\s.*\b({names})\b|\b({names})\.py\b", re.MULTILINE
        )
        for test in tests:
            if test.stem.removeprefix("test_") in stems or pattern.search(test.read_text()):
                chosen.add(test)
    return sorted(str(path.relative_to(root / API)) for path in chosen if path.exists())


def run(argv: list[str], cwd: str | None = None) -> int:
    sys.stdout.write("$ " + " ".join(argv) + "\n")
    sys.stdout.flush()
    return subprocess.run(argv, cwd=cwd, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=None)
    args = parser.parse_args(argv)
    base = base_of(args.base)
    paths = changed(base)
    selected = select(paths)
    sys.stdout.write(f"base {base[:12]}; {len(paths)} changed file(s)\n")
    status = run(["make", "lint"])
    uv = os.environ.get("UV", "uv")
    workers = os.environ.get("CHECK_WORKERS", "3")
    common = ["-m", "not loopback", "-q", "-rfE", "--tb=short", "-p", "no:cacheprovider"]
    if len(selected) > MAX_FILES:
        sys.stdout.write(f"{len(selected)} test files selected; running the whole suite\n")
        status = run(["make", "check-test"]) or status
    elif selected:
        sys.stdout.write("selected:\n" + "".join(f"  {name}\n" for name in selected))
        many = ["-n", workers] if len(selected) > 1 else []
        os.environ["CARDEA_NO_LOOPBACK"] = "1"
        status = run([uv, "run", "pytest", *common, *many, *selected], cwd=str(API)) or status
    else:
        sys.stdout.write("no test file matches the change; lint only (run `check` to be sure)\n")
    sys.stdout.write(f"test-changed: {'PASS' if status == 0 else 'FAIL'}\n")
    return 0 if status == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
