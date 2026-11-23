"""packaging/ci_annotate.py: a failed CI job's failures become check-run annotations."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "packaging" / "ci_annotate.py"

LOG = """\
uv run ruff check .
All checks passed!
tests/service/test_a.py ...F
=========================== short test summary info ============================
FAILED tests/service/test_a.py::test_one - AssertionError: assert 1 == 2
ERROR tests/service/test_b.py::test_two - sqlite3.OperationalError: 100% bad
src/cardea/x.py:3: error: Returning Any from function  [no-any-return]
make: *** [Makefile:143: api-test] Error 1
"""


def annotate(tmp_path: Path, log: str | None) -> list[str]:
    path = tmp_path / "ci.log"
    if log is not None:
        path.write_text(log)
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path)], capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def test_failures_and_the_tail_become_two_escaped_annotations(tmp_path: Path) -> None:
    failures, tail = annotate(tmp_path, LOG)
    assert failures.startswith("::error title=Failures (4)::FAILED tests/service/test_a.py")
    assert "%0AERROR tests/service/test_b.py::test_two" in failures
    assert "100%25 bad" in failures and "\n" not in failures
    assert "All checks passed" not in failures
    assert tail.startswith("::error title=Log tail::uv run ruff check .")


def test_many_failures_are_capped(tmp_path: Path) -> None:
    log = "".join(f"FAILED tests/t.py::test_{n:03d} - x\n" for n in range(100))
    failures = annotate(tmp_path, log)[0]
    assert "test_039" in failures and "test_040" not in failures
    assert failures.endswith("... and 60 more")


def test_a_missing_log_still_says_so(tmp_path: Path) -> None:
    assert annotate(tmp_path, None) == [
        "::error title=No log::The job failed before any step logged."
    ]
