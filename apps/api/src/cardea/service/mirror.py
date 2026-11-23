"""Keep the repository mirror current with what `vcs.push_branch` pushed (ADR-037, ADR-038).

`env.try` checks out a commit from the registry's `source` mirror and refuses one the mirror
does not hold (`env_commit_unknown`). A pushed pull-request head is not in the mirror until
something puts it there. After a successful push the kernel, on the host, fetches the staged
change bundle into the mirror and points the pushed branch at its head. `env.try` itself
stays offline: nothing here widens a worker's confinement, egress or credentials, and no
commit is reachable that was not pushed. Every failure is logged, never raised: the push
already happened.
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
from cardea.service.operations import OperationContext

log = logging.getLogger(__name__)

CHANGE = "/change"
SCRATCH = "refs/cardea/change"
HEAD = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
TIMEOUT = 120


def _git(mirror: Path, *args: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Git with no hooks, no credential helper, no prompts and an environment built from nothing."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    argv = [
        "git",
        "-C",
        str(mirror),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.file.allow=user",
        *args,
    ]
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def refresh(mirror: Path, bundle_file: Path, branch: str, head: str) -> bool:
    """Make `refs/heads/<branch>` of `mirror` name `head`, taken from the bundle.

    The bundle is fetched into a scratch ref and checked to be exactly `head` before the
    branch moves; the scratch ref is always removed. Returns whether the branch moved.
    """
    if not HEAD.fullmatch(head) or not BRANCH.fullmatch(branch) or ".." in branch:
        log.warning("mirror refresh refused an invalid branch or head")
        return False
    if not mirror.is_dir():
        log.warning("mirror refresh: the source is not a local repository")
        return False
    with tempfile.TemporaryDirectory(prefix="cardea-mirror-") as scratch:
        home = Path(scratch)
        try:
            listed = _git(mirror, "bundle", "list-heads", str(bundle_file), home=home)
            refs = [
                line.split(" ", 1)[1]
                for line in listed.stdout.splitlines()
                if listed.returncode == 0 and line.startswith(head + " ")
            ]
            if not refs:
                log.warning("mirror refresh: the bundle has no ref at the pushed head")
                return False
            spec = f"+{refs[0]}:{SCRATCH}"
            fetched = _git(mirror, "fetch", "-q", "--no-tags", str(bundle_file), spec, home=home)
            if fetched.returncode:
                log.warning("mirror refresh: the bundle could not be fetched into the mirror")
                return False
            found = _git(mirror, "rev-parse", "--verify", "-q", f"{SCRATCH}^{{commit}}", home=home)
            if found.returncode or found.stdout.strip() != head:
                log.warning("mirror refresh: the bundle's commit is not the pushed head")
                return False
            moved = _git(mirror, "update-ref", f"refs/heads/{branch}", head, home=home)
            if moved.returncode:
                log.warning("mirror refresh: the branch could not be updated")
                return False
            return True
        except (OSError, subprocess.SubprocessError):
            log.warning("mirror refresh: git failed", exc_info=True)
            return False
        finally:
            try:
                _git(mirror, "update-ref", "-d", SCRATCH, home=home)
            except (OSError, subprocess.SubprocessError):
                log.warning("mirror refresh: the scratch ref could not be removed")


def after_push(
    arguments: Mapping[str, JsonValue],
    context: OperationContext,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refresh the pushed repository's mirror after a successful `vcs.push_branch`."""
    try:
        registry = repositories.load(os.environ if environ is None else environ)
        alias, branch, head = (str(arguments.get(k)) for k in ("repository", "branch", "head"))
        entry = registry.repositories.get(alias) if registry else None
        bundle = context.inputs.get(CHANGE)
        if entry is None or not entry.hosted or bundle is None:
            return
        with tempfile.TemporaryDirectory(prefix="cardea-bundle-") as directory:
            bundle_file = Path(directory) / "change.bundle"
            bundle_file.write_bytes(bundle)
            refresh(Path(entry.source), bundle_file, branch, head)
    except Exception:
        log.warning("mirror refresh failed", exc_info=True)
