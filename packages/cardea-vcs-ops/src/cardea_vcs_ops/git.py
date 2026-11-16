"""Push one ref from a change bundle (ADR-037 decision 2).

The change never gets a working tree. The bundle is verified and fetched into an empty
bare repository, its head must be the commit the caller named, and exactly one ref is
pushed, without force. Every `git` command runs with hooks disabled and no credential
helper. The token reaches `git` only through `GIT_ASKPASS` and a variable in that one
child's environment; it is never on a command line, in a URL or on disk.
"""

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from cardea_vcs_ops.credentials import Credential

# Answers git's username and password prompts from the child's environment.
ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo x-access-token ;;
  *) printf '%s\\n' "$CARDEA_GIT_TOKEN" ;;
esac
"""
SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "credential.helper=")


class BundleInvalid(Exception):
    """The change bundle does not verify or does not hold the named commit."""


class PushRejected(Exception):
    """The host refused the push (for example not a fast-forward): nothing changed."""


Pusher = Callable[[bytes, str, str, str, Credential], Awaitable[None]]


def _git(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *SAFE, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _environment(work: Path, credential: Credential) -> dict[str, str]:
    askpass = work / "askpass"
    askpass.write_text(ASKPASS)
    askpass.chmod(0o700)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(work),
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "CARDEA_GIT_TOKEN": credential.token,
    }


def _push(bundle: bytes, head: str, git_url: str, branch: str, credential: Credential) -> None:
    with tempfile.TemporaryDirectory(prefix="cardea-vcs-") as directory:
        work = Path(directory)
        (work / "change.bundle").write_bytes(bundle)
        repo = work / "repo.git"
        if _git("init", "--bare", "-q", str(repo), cwd=work).returncode != 0:
            raise RuntimeError("git init failed")
        if _git("bundle", "verify", "-q", str(work / "change.bundle"), cwd=repo).returncode:
            raise BundleInvalid("the change bundle does not verify")
        listed = _git("bundle", "list-heads", str(work / "change.bundle"), cwd=repo)
        refs = [line.split(maxsplit=1) for line in listed.stdout.splitlines() if line.strip()]
        source = next((ref for sha, ref in refs if sha == head), None)
        if source is None:
            raise BundleInvalid("the change bundle has no ref at the named commit")
        fetched = _git(
            "fetch", "-q", str(work / "change.bundle"), f"{source}:refs/cardea/change", cwd=repo
        )
        if fetched.returncode != 0:
            raise BundleInvalid("the change bundle could not be read")
        if _git("rev-parse", "refs/cardea/change", cwd=repo).stdout.strip() != head:
            raise BundleInvalid("the change bundle's head is not the named commit")
        pushed = _git(
            "push",
            "--porcelain",
            git_url,
            f"{head}:refs/heads/{branch}",
            cwd=repo,
            env=_environment(work, credential),
        )
        if pushed.returncode != 0:
            if "[rejected]" in pushed.stdout or "rejected" in pushed.stderr:
                raise PushRejected("the host refused the push (not a fast-forward)")
            # Exit status only: git's stderr may quote the remote, never repeat it.
            raise RuntimeError(f"git push failed with status {pushed.returncode}")


async def push_bundle(
    bundle: bytes, head: str, git_url: str, branch: str, credential: Credential
) -> None:
    await asyncio.to_thread(_push, bundle, head, git_url, branch, credential)
