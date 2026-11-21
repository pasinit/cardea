"""Push one ref from a change bundle (ADR-037 decision 2).

The change never gets a working tree. A `Workspace` is an empty bare repository in a
private temporary directory:

1. `prepare` fetches the host's branch heads (a thin bundle such as `code.session`'s
   `base..HEAD` needs its base commits), verifies the bundle and fetches its ref at the
   named commit, which must be exactly that commit;
2. `content` returns what the push would send, for the content gate: a unified diff of the
   commits the host does not have, and their messages;
3. `push` pushes exactly one ref, without force.

Every `git` command runs with hooks disabled and no credential helper. The token reaches
`git` only through `GIT_ASKPASS` and a variable in that child's environment; it is never on
a command line, in a URL or on disk.
"""

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import TracebackType

from cardea.runtime.harness import PROXY_VARIABLES
from cardea_vcs_ops.credentials import Credential

# Answers git's username and password prompts from the child's environment.
ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo x-access-token ;;
  *) printf '%s\\n' "$CARDEA_GIT_TOKEN" ;;
esac
"""
SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "credential.helper=")
HOST = "refs/remotes/host"


class BundleInvalid(Exception):
    """The change bundle does not verify or does not hold the named commit."""


class PushRejected(Exception):
    """The host refused the push (for example not a fast-forward): nothing changed."""


class Workspace:
    def __init__(self, bundle: bytes, head: str, git_url: str, credential: Credential) -> None:
        self.bundle, self.head, self.git_url = bundle, head, git_url
        self._credential = credential
        self._directory: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> "Workspace":
        self._directory = tempfile.TemporaryDirectory(prefix="cardea-vcs-")
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._directory is not None:
            self._directory.cleanup()

    @property
    def work(self) -> Path:
        assert self._directory is not None, "use the workspace as a context manager"
        return Path(self._directory.name)

    @property
    def repo(self) -> Path:
        return self.work / "repo.git"

    def _git(self, *args: str, remote: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *SAFE, *args],
            cwd=self.repo if self.repo.exists() else self.work,
            env=self._environment()
            if remote
            else {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _environment(self) -> dict[str, str]:
        askpass = self.work / "askpass"
        if not askpass.exists():
            askpass.write_text(ASKPASS)
            askpass.chmod(0o700)
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.work),
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "CARDEA_GIT_TOKEN": self._credential.token,
            # Inside a confined worker the profile's egress proxy is the only route out.
            **{name: os.environ[name] for name in PROXY_VARIABLES if name in os.environ},
        }

    def prepare(self) -> None:
        bundle = self.work / "change.bundle"
        bundle.write_bytes(self.bundle)
        if self._git("init", "--bare", "-q", str(self.repo)).returncode != 0:
            raise RuntimeError("git init failed")
        # The host's heads, so the bundle's prerequisites (its base) are present.
        fetched = self._git("fetch", "-q", self.git_url, f"+refs/heads/*:{HOST}/*", remote=True)
        if fetched.returncode != 0:
            raise RuntimeError(f"git fetch from the host failed with status {fetched.returncode}")
        if self._git("bundle", "verify", "-q", str(bundle)).returncode:
            raise BundleInvalid("the change bundle does not verify against the repository")
        listed = self._git("bundle", "list-heads", str(bundle))
        refs = [line.split(maxsplit=1) for line in listed.stdout.splitlines() if line.strip()]
        source = next((ref for sha, ref in refs if sha == self.head), None)
        if source is None:
            raise BundleInvalid("the change bundle has no ref at the named commit")
        if self._git("fetch", "-q", str(bundle), f"{source}:refs/cardea/change").returncode:
            raise BundleInvalid("the change bundle could not be read")
        if self._git("rev-parse", "refs/cardea/change").stdout.strip() != self.head:
            raise BundleInvalid("the change bundle's head is not the named commit")

    def content(self) -> tuple[str, dict[str, str]]:
        """The commits the host does not have yet: their added lines and messages."""
        new = (self.head, "--not", f"--remotes={HOST.removeprefix('refs/remotes/')}")
        diff = self._git("log", "-p", "--no-color", "--format=", "--reverse", *new)
        logged = self._git("log", "--format=%H%x00%B%x00", *new)
        if diff.returncode or logged.returncode:
            raise BundleInvalid("the change bundle's history could not be read")
        parts = logged.stdout.split("\x00")
        messages = {
            f"commit_message:{sha.strip()[:12]}": body.strip()
            for sha, body in zip(parts[0::2], parts[1::2], strict=False)
            if sha.strip()
        }
        return diff.stdout, messages

    def push(self, branch: str) -> None:
        pushed = self._git(
            "push", "--porcelain", self.git_url, f"{self.head}:refs/heads/{branch}", remote=True
        )
        if pushed.returncode != 0:
            if "[rejected]" in pushed.stdout or "rejected" in pushed.stderr:
                raise PushRejected("the host refused the push (not a fast-forward)")
            # Exit status only: git's stderr may quote the remote; never repeat it.
            raise RuntimeError(f"git push failed with status {pushed.returncode}")


Pusher = Callable[[Workspace, str], Awaitable[None]]


async def push(workspace: Workspace, branch: str) -> None:
    await asyncio.to_thread(workspace.push, branch)
