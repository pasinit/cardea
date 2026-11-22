"""Attach a failed CI job's failures to its check run, as annotations.

Usage:  python3 packaging/ci_annotate.py LOG

Run as the last step of a CI job, only on failure, with LOG the job's teed output. It
prints GitHub workflow commands: one annotation with the lines that name a failure (failed
test ids, type and lint errors), and one with the log's tail. Agents read them through
`vcs.read_change` (its `checks`), since they cannot read the job logs themselves.
"""

import re
import sys
from pathlib import Path

# Lines that name a failure: pytest's short summary, mypy, ruff, make.
FAILURE = re.compile(
    r"^(FAILED|ERROR) |: error: |^[^\s:]+:\d+:\d+: [A-Z]+\d+ |^make(\[\d+\])?: \*\*\* "
)
MAX_LINES = 40
TAIL = 30
MAX_LINE = 300


def escape(text: str) -> str:
    """A workflow command's message: `%`, CR and LF encoded."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotations(log: str) -> list[str]:
    lines = [line[:MAX_LINE] for line in log.splitlines()]
    named = [line for line in lines if FAILURE.search(line)]
    out = []
    if named:
        shown = named[:MAX_LINES]
        more = f"\n... and {len(named) - len(shown)} more" if len(named) > len(shown) else ""
        out.append(f"::error title=Failures ({len(named)})::{escape(chr(10).join(shown) + more)}")
    tail = [line for line in lines if line.strip()][-TAIL:]
    if tail:
        out.append(f"::error title=Log tail::{escape(chr(10).join(tail))}")
    return out


def main() -> None:
    path = Path(sys.argv[1])
    log = path.read_text(errors="replace") if path.exists() else ""
    fallback = "::error title=No log::The job failed before any step logged."
    for command in annotations(log) or [fallback]:
        print(command)


if __name__ == "__main__":
    main()
