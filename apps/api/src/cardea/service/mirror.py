"""Keep the repository mirror current after a push (ADR-037).

`env.try` checks a commit out of the registry's `source`, offline. A pull request's head
is in that mirror only once something put it there: after a successful `vcs.push_branch`
the kernel fetches the pushed branch from the change bundle it already staged for the
push. No network, no credential and no new confinement are involved, and `env.try` still
refuses a SHA the mirror does not hold.

Best effort: a failure here is logged and never changes the push's result.
"""

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from cardea.service import repositories
from cardea.service.database import Fault
from cardea.service.operations import OperationContext

log = logging.getLogger(__name__)

SCRATCH = "refs/cardea/change"
HEAD = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
TIMEOUT = 120


class MirrorRefused(Exception):
    """The mirror was left as it was."""


def valid_branch(branch: str) -> bool:
    return (
        BRANCH.fullmatch(branch) is not None
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
        and "/." not in branch
    )


def _git(mirror: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Git with hooks, credentials, global configuration and prompts all off."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    argv = [
        "git",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "credential.helper=",
        "-c",
        "protocol.file.allow=user",
        "-C",
        str(mirror),
        *args,
    ]
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def refresh(mirror: Path, bundle_file: Path, branch: str, head: str) -> None:
    """Point `refs/heads/<branch>` of `mirror` at `head`, fetched from the bundle.

    Raises `MirrorRefused` when anything does not check out; the scratch ref is always
    removed.
    """
    if not HEAD.fullmatch(head):
        raise MirrorRefused("head is not a full object ID")
    if not valid_branch(branch):
        raise MirrorRefused("branch name is not acceptable")
    if not mirror.is_dir():
        raise MirrorRefused("the mirror is not a local directory")
    with tempfile.TemporaryDirectory(prefix="cardea-mirror-") as scratch:
        home = Path(scratch)

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return _git(mirror, home, *args)

        heads = run("bundle", "list-heads", str(bundle_file))
        if heads.returncode:
            raise MirrorRefused("the change bundle cannot be read")
        refs = [line.split(" ", 1) for line in heads.stdout.splitlines() if " " in line]
        named = [ref for sha, ref in refs if sha == head]
        if not named:
            raise MirrorRefused("the change bundle does not carry the pushed head")
        try:
            spec = f"+{named[0]}:{SCRATCH}"
            fetched = run("fetch", "--quiet", "--no-tags", str(bundle_file), spec)
            if fetched.returncode:
                raise MirrorRefused("the change bundle could not be fetched")
            found = run("rev-parse", "--verify", "--quiet", SCRATCH + "^{commit}")
            if found.returncode or found.stdout.strip() != head:
                raise MirrorRefused("the fetched commit is not the pushed head")
            checked_out = run("symbolic-ref", "--quiet", "HEAD")
            if checked_out.stdout.strip() == f"refs/heads/{branch}":
                raise MirrorRefused("the branch is checked out in the mirror")
            updated = run("update-ref", f"refs/heads/{branch}", head)
            if updated.returncode:
                raise MirrorRefused("the branch could not be updated")
        finally:
            run("update-ref", "-d", SCRATCH)


def after_push(
    arguments: dict[str, JsonValue],
    context: OperationContext,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refresh the mirror of the repository a push just reached. Never raises."""
    try:
        registry = repositories.load(os.environ if environ is None else environ)
        entry = registry.repositories.get(str(arguments.get("repository"))) if registry else None
        if entry is None or not entry.hosted:
            return
        bundle = context.inputs.get("/change")
        if bundle is None:
            return
        with tempfile.TemporaryDirectory(prefix="cardea-bundle-") as folder:
            path = Path(folder) / "change.bundle"
            path.write_bytes(bundle)
            refresh(Path(entry.source), path, str(arguments["branch"]), str(arguments["head"]))
    except MirrorRefused as refused:
        log.warning("mirror not refreshed: %s", refused, extra={"code": "mirror_refused"})
    except (Fault, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        log.warning(
            "mirror not refreshed (%s)", type(error).__name__, extra={"code": "mirror_failed"}
        )
