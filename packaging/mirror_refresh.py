"""Keep the host's repository mirrors holding what `env.try` is asked to run.

Usage, as the host owner, outside every sandbox (the kit's `cardea-mirror-refresh.timer`):

    python3 packaging/mirror_refresh.py REGISTRY [--repo ALIAS] [--commit SHA]

Each hosted repository of the registry (one with a `git_url`) has a bare mirror at its
`source`. A plain `git fetch` brings only the branches, so a pull request head, which lives
under `refs/pull/*/head`, is missing from the mirror and `env.try` refuses it as
`env_commit_unknown`. This fetches, into the mirror:

- every branch, `agent/*` included (`+refs/heads/*:refs/heads/*`);
- every pull request head (`+refs/pull/*/head:refs/pull/*/head`);
- with `--commit`, that one full object ID, kept under `refs/cardea/fetched/<sha>` so a
  collection does not drop it. The host refuses an ID it does not have, and so does this.

It runs where the owner runs it, not in the `try` worker: the worker's mirror stays
read-only and its egress stays the proxy's allow-list. The fetch is anonymous (no credential
helper, no prompt, no global configuration), so a private repository is not refreshed here
and no credential is held or written.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

COMMIT = re.compile(r"^[0-9a-f]{40}$")
REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/pull/*/head:refs/pull/*/head")
FETCHED = "refs/cardea/fetched"


def _git(mirror: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(mirror),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(mirror), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def refresh(mirror: Path, git_url: str, commit: str | None = None) -> None:
    """Fetch the branches, the pull request heads and optionally one commit into `mirror`."""
    if commit is not None and not COMMIT.fullmatch(commit):
        raise ValueError("commit must be a full 40-hex object ID")
    if _git(mirror, "rev-parse", "--is-bare-repository").stdout.strip() != "true":
        raise ValueError(f"{mirror} is not a bare repository")
    # A host without pull request refs (or a mirror of a plain server) has none to fetch.
    for spec in REFSPECS:
        fetched = _git(mirror, "fetch", "--quiet", "--no-tags", git_url, spec)
        if fetched.returncode and spec in REFSPECS[:1]:
            raise RuntimeError(f"fetching branches failed: {fetched.stderr.strip()}")
    if commit is not None:
        spec = f"{commit}:{FETCHED}/{commit}"
        if _git(mirror, "fetch", "--quiet", "--no-tags", git_url, spec).returncode:
            raise LookupError(f"the repository host does not have {commit}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("registry", type=Path)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--commit", default=None)
    args = parser.parse_args(argv)
    if args.commit is not None and args.repo is None:
        parser.error("--commit needs --repo")
    entries = json.loads(args.registry.read_text())["repositories"]
    failed = 0
    for alias, entry in sorted(entries.items()):
        if args.repo not in (None, alias) or not entry.get("git_url"):
            continue
        try:
            refresh(Path(entry["source"]), entry["git_url"], args.commit)
        except (ValueError, RuntimeError, LookupError, OSError) as error:
            print(f"{alias}: {error}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
