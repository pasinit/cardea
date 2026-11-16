"""`code.session` inside its confined worker (ADR-037 slice 3).

One call, no state kept between calls:

1. clone `base_ref` of the repository named by its alias from the host owner's read-only
   source into a scratch directory, with every git hook and global configuration off;
2. run the configured headless coding agent (the harness) in the checkout, with the task;
3. commit what it changed, bundle the commits from `base_ref` and summarize the diff;
4. run each configured gate in the checkout and record its exit status and output tail;
5. return the bounded summary and the bundle as the `change.bundle` artifact.

Configuration is a host-owner JSON file named by `CARDEA_CODING_CONFIG` (read-only in the
worker): repositories by alias with their read-only `source` and `gates`, and the `harness`
command. A call names only an alias, never a path or a command.

Every command runs with an environment built from nothing: a fixed `PATH`, a scratch
`HOME`, no global git configuration, plus only the variables the harness entry passes
through by name (its model credential). Names that could carry a repository-host
credential cannot be passed through, so no such credential exists in the worker's commands
even when the worker's own environment had one.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from cardea.service.operations import OperationContext, OperationRejected, WithArtifacts

CONFIG = "CARDEA_CODING_CONFIG"
BUNDLE = "change.bundle"
MAX_DIFF_EXCERPT = 16384
MAX_TAIL = 4096
DEFAULT_GATE_TIMEOUT = 600
DEFAULT_HARNESS_TIMEOUT = 1800
PATH = "/usr/local/bin:/usr/bin:/bin"
# Never passed to a command: anything that could hold a repository-host credential.
CREDENTIAL_NAMES = re.compile(
    r"^(GH_|GITHUB_|GIT_|GITLAB_|GITEA_)|ASKPASS|CREDENTIAL|^SSH_AUTH_SOCK$|^NETRC$"
)
AUTHOR = ["-c", "user.name=cardea", "-c", "user.email=code-session@cardea.invalid"]


def _config() -> dict[str, Any]:
    path = os.environ.get(CONFIG)
    if not path:
        raise OperationRejected("code.session is not configured on this host")
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict):
        raise OperationRejected("code.session configuration is not an object")
    return document


def environment(scratch: Path, passthrough: list[str] | None = None) -> dict[str, str]:
    """A command's whole environment: fixed values plus named, credential-free passthrough."""
    home = scratch / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": PATH,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(scratch),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in passthrough or ():
        if CREDENTIAL_NAMES.search(name):
            raise OperationRejected(f"the harness may not receive {name}")
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _tail(text: str) -> str:
    return text[-MAX_TAIL:]


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout


def _command(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None
) -> dict[str, JsonValue]:
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code: int | None = done.returncode
        output = done.stdout + done.stderr
        timed_out = False
    except subprocess.TimeoutExpired as expired:
        exit_code, timed_out = None, True
        output = (
            (expired.stdout or b"").decode(errors="replace")
            if isinstance(expired.stdout, bytes)
            else str(expired.stdout or "")
        )
    except OSError as error:
        exit_code, timed_out, output = None, False, f"could not start: {type(error).__name__}"
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output_tail": _tail(output),
    }


def session(arguments: dict[str, JsonValue], config: dict[str, Any], scratch: Path) -> Any:
    """The whole call in `scratch`; returns the output and, if anything changed, the bundle."""
    alias = str(arguments["repository"])
    repository = config.get("repositories", {}).get(alias)
    if not isinstance(repository, dict):
        raise OperationRejected(f"repository {alias} is not allowed on this host")
    base_ref = str(arguments["base_ref"])
    env = environment(scratch)
    repo = scratch / "repo"
    clone = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--single-branch",
            "--branch",
            base_ref,
            str(repository["source"]),
            str(repo),
        ],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if clone.returncode:
        raise OperationRejected(f"base_ref {base_ref} could not be checked out")
    base_sha = _git(repo, env, "rev-parse", "HEAD").strip()

    harness = config.get("harness", {})
    harness_env = environment(scratch, list(harness.get("passthrough", ())))
    harness_env["CARDEA_TASK"] = str(arguments["task"])
    ran = _command(
        [str(part) for part in harness["argv"]],
        repo,
        harness_env,
        float(harness.get("timeout_seconds", DEFAULT_HARNESS_TIMEOUT)),
        stdin=str(arguments["task"]),
    )

    _git(repo, env, "add", "--all")
    changed = bool(_git(repo, env, "status", "--porcelain").strip())
    head_sha: str | None = None
    files = insertions = deletions = 0
    diff = ""
    bundle = b""
    if changed:
        _git(repo, env, *AUTHOR, "commit", "--quiet", "--no-verify", "-m", "code.session change")
        head_sha = _git(repo, env, "rev-parse", "HEAD").strip()
        for line in _git(repo, env, "diff", "--numstat", base_sha, head_sha).splitlines():
            added, removed, _ = line.split("\t", 2)
            files += 1
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        diff = _git(repo, env, "diff", base_sha, head_sha)
        target = scratch / BUNDLE
        _git(repo, env, "bundle", "create", "--quiet", str(target), f"{base_sha}..HEAD")
        bundle = target.read_bytes()

    gates: list[JsonValue] = []
    passed: list[bool] = []
    for gate in repository.get("gates", ()):
        result = _command(
            [str(part) for part in gate["argv"]],
            repo,
            environment(scratch),
            float(gate.get("timeout_seconds", DEFAULT_GATE_TIMEOUT)),
        )
        passed.append(result["exit_code"] == 0)
        gates.append({"name": str(gate["name"]), "passed": passed[-1], **result})

    output: dict[str, JsonValue] = {
        "repository": alias,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed": changed,
        "files_changed": files,
        "insertions": insertions,
        "deletions": deletions,
        "diff_excerpt": diff[:MAX_DIFF_EXCERPT],
        "diff_truncated": len(diff) > MAX_DIFF_EXCERPT,
        "harness": ran,
        "gates": gates,
        "gates_passed": all(passed),
    }
    if not changed:
        return output
    return WithArtifacts(output=output, artifacts={BUNDLE: bundle})


async def run(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    """The confined worker's entry point (`coding.code.session`)."""
    config = _config()
    scratch = Path(tempfile.mkdtemp(prefix="code-session-"))
    try:
        return session(arguments, config, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
