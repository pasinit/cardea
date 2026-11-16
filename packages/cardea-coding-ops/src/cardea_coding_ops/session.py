"""`code.session` inside its confined worker (ADR-037 slice 3).

One call, no state kept between calls:

1. clone `base_ref` of the repository named by its alias from the host owner's read-only
   source into a scratch directory, with every git hook and global configuration off;
2. run the configured headless coding agent (the harness) in the checkout, with the task;
3. commit what it changed, bundle the commits from `base_ref` and summarize the diff;
4. run each configured gate in the checkout and record its exit status and output tail;
5. return the bounded summary and the bundle as the `change.bundle` artifact.

Configuration is a host-owner JSON file named by `CARDEA_CODING_CONFIG` (read-only in the
worker): the `harness` command and the gates' environment. Repositories come from the
host's repository registry (`CARDEA_REPOSITORIES`, `cardea.service.repositories`): each
alias's read-only `source` and its `gates`. For one release this file may still list
`repositories` itself, with a deprecation warning. A call names only an alias, never a path
or a command.

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

from cardea.service import repositories
from cardea.service.database import Fault
from cardea.service.operations import OperationContext, OperationRejected, WithArtifacts
from cardea_coding_ops import claude_code

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


def model_credential() -> str | None:
    """The profile's model credential, if the worker has one (ADR-028)."""
    path = os.environ.get("CARDEA_CREDENTIAL")
    if not path or not Path(path).is_file():
        return None
    return Path(path).read_text().strip() or None


def fixed_env(values: object) -> dict[str, str]:
    """Host-owner values for the gates, such as a read-only package cache with no network
    (`UV_CACHE_DIR`, `UV_OFFLINE=1`, `PIP_NO_INDEX=1`); never a credential-shaped name."""
    if not isinstance(values, dict):
        raise OperationRejected("gate_env must be an object")
    env: dict[str, str] = {}
    for name, value in values.items():
        # Proxy variables come only from the profile, through `dependencies: registries`.
        if (
            not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(name))
            or CREDENTIAL_NAMES.search(str(name))
            or str(name).endswith("_PROXY")
        ):
            raise OperationRejected(f"gate_env may not set {name}")
        env[str(name)] = str(value)
    return env


# How the gates get dependencies (ADR-037 question 8), chosen by `dependencies` in the
# configuration:
# - `cache` (the default, and the stricter option): no network at all for the gates; they
#   use the read-only, pre-provisioned cache that `gate_env` points at;
# - `registries`: the gates receive the worker's egress proxy variables, so they may reach
#   the package registries the `coding` profile allows (its `registries`, port 443 only).
#   The proxy, not this setting, decides which hosts that is.
DEPENDENCIES = ("cache", "registries")
PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy")
# The helper's egress proxies listen on 127.0.10.0/24 (ADR-028); nothing else is accepted.
PROXY_URL = re.compile(r"http://127\.0\.10\.\d{1,3}:\d{1,5}")


def dependency_env(config: dict[str, Any]) -> dict[str, str]:
    """The proxy variables the gates get: none for `cache`, the worker's for `registries`."""
    mode = config.get("dependencies", "cache")
    if mode not in DEPENDENCIES:
        raise OperationRejected(f"dependencies must be one of {', '.join(DEPENDENCIES)}")
    if mode == "cache":
        return {}
    env = {name: os.environ[name] for name in PROXY_NAMES if name in os.environ}
    proxies = [v for k, v in env.items() if k.lower() in {"https_proxy", "http_proxy"}]
    if not proxies or not all(PROXY_URL.fullmatch(v) for v in proxies):
        raise OperationRejected("dependencies from registries need the profile's egress proxy")
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


def _claude(
    settings: dict[str, Any], repo: Path, scratch: Path, task: str, timeout: float
) -> dict[str, JsonValue]:
    """Run Claude Code headless on the task; its JSON result becomes the harness record."""
    env = claude_code.environment(environment(scratch), scratch, settings)
    started = time.monotonic()
    try:
        done = subprocess.run(
            claude_code.argv(settings),
            cwd=repo,
            env=env,
            input=task,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code: int | None = done.returncode
        timed_out, stdout = False, done.stdout
    except subprocess.TimeoutExpired:
        exit_code, timed_out, stdout = None, True, ""
    except OSError:
        exit_code, timed_out, stdout = None, False, ""
    # Only the parsed result is kept: stdout and stderr are not copied into the receipt.
    return {
        "kind": "claude-code",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        **claude_code.result(stdout),
    }


def repository_entry(alias: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """The alias's `source` and `gates`: from the host's repository registry
    (`CARDEA_REPOSITORIES`), or, deprecated for one release, from this file's
    `repositories`. Both set and disagreeing is refused."""
    try:
        registry = repositories.resolve("code.session", config.get("repositories"))
    except Fault as error:
        raise OperationRejected(error.message, error.code) from None
    if registry is None:
        legacy = config.get("repositories", {}).get(alias)
        return legacy if isinstance(legacy, dict) else None
    entry = registry.repositories.get(alias)
    if entry is None:
        return None
    return {"source": entry.source, "gates": entry.gate_documents()}


def session(arguments: dict[str, JsonValue], config: dict[str, Any], scratch: Path) -> Any:
    """The whole call in `scratch`; returns the output and, if anything changed, the bundle."""
    alias = str(arguments["repository"])
    repository = repository_entry(alias, config)
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
    timeout = float(harness.get("timeout_seconds", DEFAULT_HARNESS_TIMEOUT))
    ran: dict[str, JsonValue]
    if harness.get("kind") == "claude-code":
        ran = _claude(harness, repo, scratch, str(arguments["task"]), timeout)
    else:
        harness_env = environment(scratch, list(harness.get("passthrough", ())))
        harness_env["CARDEA_TASK"] = str(arguments["task"])
        ran = _command(
            [str(part) for part in harness["argv"]],
            repo,
            harness_env,
            timeout,
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
    gate_env = environment(scratch) | dependency_env(config) | fixed_env(config.get("gate_env", {}))
    for gate in repository.get("gates", ()):
        result = _command(
            [str(part) for part in gate["argv"]],
            repo,
            gate_env,
            float(gate.get("timeout_seconds", DEFAULT_GATE_TIMEOUT)),
        )
        passed.append(result["exit_code"] == 0)
        gates.append({"name": str(gate["name"]), "passed": passed[-1], **result})

    # The model credential must not leave the worker in the change or any result text. The
    # whole change is checked as text (`--text`), so binary files cannot hide it.
    secret = model_credential()
    if secret:
        everything = json.dumps([ran, gates])
        if changed:
            everything += _git(repo, env, "diff", "--text", base_sha, str(head_sha))
        if secret in everything:
            raise OperationRejected(
                "the change or a command's output contains the model credential; nothing is "
                "returned",
                "credential_in_change",
            )

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
