"""The `check` scenario's runner for `env.try` (ADR-038): lint, typecheck and the unit tests
that do not need TCP loopback, one report.

Usage (`make check`):

    python3 packaging/check.py

Runs `make lint`, `make typecheck` and `make check-test` in order, every step even after a
failure, and streams their output. It ends with a summary that fits in `env.try`'s 4 KiB log
tail: each step's outcome and exit status, the failing test ids (pytest's `FAILED` and
`ERROR` lines, capped, with the rest counted) and a last line `check: PASS (exit 0)` or
`check: FAIL (exit N)`. The process exits with that status, the first failing step's, so the
scenario's exit code and the log tail agree. Nothing here widens what the scenario can reach:
it runs the same commands as the `lint`, `typecheck` and `api-test` scenarios, in the same
confinement and with the same egress.
"""

import re
import subprocess
import sys
from collections.abc import Sequence

STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lint", ("make", "lint")),
    ("typecheck", ("make", "typecheck")),
    ("test", ("make", "check-test")),
)
# pytest's short summary (`-rfE`): `FAILED path::test[param] - message`.
FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+(?: \S+)*?)(?: - .*)?$", re.MULTILINE)
# Leaves room in the 4 KiB tail for the step lines and the final line.
MAX_IDS = 20
MAX_ID_LENGTH = 200


def failing_ids(output: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in FAILED.finditer(output):
        seen.setdefault(match.group(1)[:MAX_ID_LENGTH])
    return list(seen)


def run_step(argv: Sequence[str]) -> tuple[int, str]:
    """One command's exit status and merged output, streamed as it arrives."""
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
    except OSError as error:
        message = f"could not start {argv[0]}: {error}\n"
        sys.stdout.write(message)
        return 127, message
    assert process.stdout is not None
    lines: list[str] = []
    for line in process.stdout:
        sys.stdout.write(line)
        lines.append(line)
    return process.wait(), "".join(lines)


def summary(results: Sequence[tuple[str, int]], ids: Sequence[str]) -> tuple[str, int]:
    status = next((code for _, code in results if code), 0)
    lines = ["", "== check summary =="]
    lines += [f"{name}: {'pass' if code == 0 else 'FAIL'} (exit {code})" for name, code in results]
    if ids:
        lines.append("failing tests:")
        lines += [f"  {i}" for i in ids[:MAX_IDS]]
        if len(ids) > MAX_IDS:
            lines.append(f"  ... and {len(ids) - MAX_IDS} more")
    lines.append(f"check: {'PASS' if status == 0 else 'FAIL'} (exit {status})")
    return "\n".join(lines) + "\n", status


def main(steps: Sequence[tuple[str, Sequence[str]]] = STEPS) -> int:
    results: list[tuple[str, int]] = []
    ids: list[str] = []
    for name, argv in steps:
        code, output = run_step(argv)
        results.append((name, code))
        if name == "test":
            ids = failing_ids(output)
    text, status = summary(results, ids)
    sys.stdout.write(text)
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    sys.exit(main())
