"""The session's own checkout (ADR-037 amendment 2026-11-24).

A run with a workspace gets, before its session starts, each named repository cloned from
the host owner's read-only source into the session's scratch directory, and a
`cardea-change` command on `PATH`. The agent edits and commits there with its built-in
tools. Testing, pushing and pull requests stay installed operations: `cardea-change`
bundles the commits on top of the clone's base and uploads them to the run's loopback
endpoint. The kernel stores the bundle as the run's own objects, which `env.try` and
`vcs.push_branch` take as `change`.

The clone runs with hooks disabled and no system or global git configuration, so the
repository's own content cannot run anything here. The agent's shell is confined by the
worker, not by this module.
"""

import os
import subprocess
import sys
from pathlib import Path

from cardea.service import repositories
from cardea.service.database import Fault

# The clone's starting commit; `cardea-change` bundles what the agent committed on top.
BASE_REF = "refs/cardea/base"
BIN = ".cardea/bin"
AUTHOR = ("Cardea agent", "agent@cardea.invalid")


class WorkspaceRefused(Exception):
    """The workspace could not be prepared; the session does not start."""


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def prepare(workdir: Path, aliases: tuple[str, ...]) -> list[Path]:
    """Clone each alias from its registry `source` into `workdir/<alias>`; returns the
    checkouts. Refuses an alias the host's registry does not list."""
    try:
        registry = repositories.load()
    except Fault as invalid:
        raise WorkspaceRefused(f"workspace_registry_invalid: {invalid.code}") from None
    if registry is None:
        raise WorkspaceRefused("workspace_registry_missing")
    checkouts: list[Path] = []
    for alias in aliases:
        entry = registry.repositories.get(alias)
        if entry is None:
            raise WorkspaceRefused(f"workspace_repository_unknown: {alias}")
        target = workdir / alias
        cloned = _git(workdir, "clone", "--quiet", "--no-hardlinks", entry.source, str(target))
        if cloned.returncode:
            raise WorkspaceRefused(f"workspace_clone_failed: {alias}")
        for key, value in (
            ("core.hooksPath", os.devnull),
            ("user.name", AUTHOR[0]),
            ("user.email", AUTHOR[1]),
        ):
            _git(target, "config", key, value)
        if _git(target, "update-ref", BASE_REF, "HEAD").returncode:
            raise WorkspaceRefused(f"workspace_clone_empty: {alias}")
        checkouts.append(target)
    tool = workdir / BIN / "cardea-change"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(f"#!{sys.executable} -I\n" + CHANGE_TOOL)
    tool.chmod(0o700)
    return checkouts


def environment(workdir: Path, upload_url: str, token: str) -> dict[str, str]:
    """What the session's shell needs for `cardea-change`: the command on `PATH` and the
    run's upload route with its bearer token (the same token the run's tools use, so it
    grants nothing the session does not already hold)."""
    return {
        "PATH": f"{workdir / BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "CARDEA_UPLOAD_URL": upload_url,
        "CARDEA_UPLOAD_TOKEN": token,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def instructions(aliases: tuple[str, ...]) -> str:
    """Appended to the session's system prompt: how the workspace and operations meet."""
    listed = ", ".join(f"./{alias}" for alias in aliases)
    return (
        "\n\nYour workspace: " + listed + " (fresh clones of the default branch). Edit, read and "
        "commit there with your shell and file tools. You have no network and no repository "
        "credential. To test, push or open a pull request, run `cardea-change` inside the "
        "repository: it bundles your commits since the clone and prints JSON with `change`, "
        "`head` and `base`. Pass `change` and `head` to the operations: env.try "
        '{"repo", "commit": head, "change", "scenarios"} runs the tests in a separate '
        'environment and returns per-scenario results and log tails; vcs.push_branch {"change", '
        '"head", ...} pushes it. Read every result and fix what failed before moving on. To '
        "continue an existing branch, check it out first (`git checkout -B work "
        "origin/<branch>`): the clone holds the source's branches, and `git fetch` is "
        "unavailable."
    )


# The `cardea-change` command (stdlib only, run with the worker's Python).
CHANGE_TOOL = r'''
"""cardea-change [path]: bundle the commits made since the workspace clone and hand them
to the run's operations. Prints {"change", "head", "base", "repo"} as JSON."""
import json, os, subprocess, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

def git(*args):
    done = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=root,
                          capture_output=True, text=True, check=False)
    if done.returncode:
        sys.exit(f"cardea-change: git {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()

start = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
found = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                       capture_output=True, text=True, check=False)
if found.returncode:
    sys.exit("cardea-change: run it inside a workspace repository")
root = Path(found.stdout.strip())
base, head = git("rev-parse", "refs/cardea/base"), git("rev-parse", "HEAD")
if base == head:
    sys.exit("cardea-change: no commits since the clone; commit your changes first")
if git("status", "--porcelain"):
    print("cardea-change: warning: uncommitted changes are not included", file=sys.stderr)
with tempfile.TemporaryDirectory() as scratch:
    path = Path(scratch) / "change.bundle"
    git("bundle", "create", "--quiet", str(path), "refs/cardea/base..HEAD")
    content = path.read_bytes()
request = urllib.request.Request(
    os.environ["CARDEA_UPLOAD_URL"], data=content, method="POST",
    headers={"Authorization": "Bearer " + os.environ["CARDEA_UPLOAD_TOKEN"],
             "Content-Type": "application/octet-stream"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(request, timeout=120) as reply:
        listing = json.load(reply)
except urllib.error.HTTPError as error:
    detail = error.read()[:300].decode(errors="replace")
    sys.exit(f"cardea-change: upload refused ({error.code}): {detail}")
print(json.dumps({"change": listing, "head": head, "base": base, "repo": root.name}))
'''
