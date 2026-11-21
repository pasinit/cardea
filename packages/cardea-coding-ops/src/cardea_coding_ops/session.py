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

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from cardea.hosts import isolation
from cardea.runtime.harness import PROXY_VARIABLES
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
    """Host-owner values for the gates, such as a read-only package cache used offline
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
# configuration. The proxy, not this setting, decides which hosts can be reached. The
# consequence: whatever the profile's `registries` allow, every process in the unit can
# reach, whatever this says. The harness (Claude Code) runs with the proxy variables in
# either mode, and code in the checkout can name the proxy address itself. So:
# - `registries`: the profile lists registries, and the gates get the proxy variables;
# - `cache`: the profile lists none (its proxy allows only the model API), and the gates
#   use the read-only, pre-provisioned cache that `gate_env` points at. This is the
#   stricter option only together with that profile.
# The helper tells the worker what its profile allows (`CARDEA_REGISTRIES`, launcher), and
# a setting that disagrees is refused with `dependencies_mismatch` before anything runs.
DEPENDENCIES = ("cache", "registries")
PROXY_NAMES = PROXY_VARIABLES
# The helper's egress proxies listen on 127.0.10.0/24 (ADR-028); nothing else is accepted.
PROXY_URL = re.compile(r"http://127\.0\.10\.\d{1,3}:\d{1,5}")
REGISTRIES_ENV = "CARDEA_REGISTRIES"


def profile_registries() -> list[str] | None:
    """The registries the worker's profile allows, as the helper set them; None if not told
    (a unit without network, or a worker not started by the helper)."""
    value = os.environ.get(REGISTRIES_ENV)
    if value is None:
        return None
    return [host for host in value.split(",") if host]


def dependency_env(config: dict[str, Any]) -> dict[str, str]:
    """The proxy variables the gates get: none for `cache`, the worker's for `registries`.

    Refuses, as `dependencies_mismatch`, a `cache` setting on a profile whose proxy allows
    registries (the cache would not be the stricter option it claims), and a `registries`
    setting on a profile that allows none.
    """
    mode = config.get("dependencies", "cache")
    if mode not in DEPENDENCIES:
        raise OperationRejected(f"dependencies must be one of {', '.join(DEPENDENCIES)}")
    allowed = profile_registries()
    if mode == "cache":
        if allowed:
            raise OperationRejected(
                "dependencies is cache, but this worker's profile allows package registries "
                f"({', '.join(allowed)}); remove them from the profile and the proxy, or set "
                "dependencies to registries",
                code="dependencies_mismatch",
            )
        return {}
    if not allowed:
        raise OperationRejected(
            "dependencies is registries, but this worker's profile allows no package registry",
            code="dependencies_mismatch",
        )
    env = {name: os.environ[name] for name in PROXY_NAMES if name in os.environ}
    proxies = [v for k, v in env.items() if k.lower() in {"https_proxy", "http_proxy"}]
    if not proxies or not all(PROXY_URL.fullmatch(v) for v in proxies):
        raise OperationRejected("dependencies from registries need the profile's egress proxy")
    return env


def _tail(text: str) -> str:
    return text[-MAX_TAIL:]


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        # A change may hold binary or non-UTF-8 files, and `diff --text` prints them raw.
        # Strict decoding raised UnicodeDecodeError and lost the whole result; replacing
        # undecodable bytes keeps every ASCII run, so the credential scan still sees a token.
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout


def _uncontained(error: isolation.IsolationFailed) -> OperationRejected:
    """A step's process survived, or the worker could not be hardened: report nothing."""
    return OperationRejected(f"untrusted steps not contained: {error}", "isolation_failed")


def _step(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None
) -> isolation.Step:
    """One untrusted command (the harness or a gate) in its own session; whatever it leaves
    behind is killed before the next step, and a survivor refuses the call."""
    try:
        return isolation.run_step(argv, cwd, env, timeout, stdin)
    except isolation.IsolationFailed as error:
        raise _uncontained(error) from None


def _command(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None
) -> dict[str, JsonValue]:
    done = _step(argv, cwd, env, timeout, stdin)
    return {
        "exit_code": done.returncode,
        "timed_out": done.timed_out,
        "duration_ms": done.duration_ms,
        "output_tail": _tail(done.stdout + done.stderr),
        "stray_processes_killed": done.killed,
    }


def _claude(
    settings: dict[str, Any], repo: Path, scratch: Path, task: str, timeout: float
) -> dict[str, JsonValue]:
    """Run Claude Code headless on the task; its JSON result becomes the harness record."""
    env = claude_code.environment(environment(scratch), scratch, settings)
    done = _step(claude_code.argv(settings), repo, env, timeout, task)
    # Only the parsed result is kept: stdout and stderr are not copied into the receipt.
    return {
        "kind": "claude-code",
        "exit_code": done.returncode,
        "timed_out": done.timed_out,
        "duration_ms": done.duration_ms,
        "stray_processes_killed": done.killed,
        **claude_code.result(done.stdout if not done.timed_out else ""),
    }


def checkout_state(repo: Path, env: dict[str, str]) -> dict[str, str]:
    """What the change is, as far as the gates could touch it: the commit and its refs, the
    tracked files and the repository's configuration."""
    config = repo / ".git" / "config"
    return {
        "head": _git(repo, env, "rev-parse", "HEAD").strip(),
        "refs": _git(repo, env, "for-each-ref", "--format=%(refname) %(objectname)"),
        "tracked_changes": _git(repo, env, "status", "--porcelain", "--untracked-files=no"),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest()
        if config.is_file()
        else "",
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


def session(
    arguments: dict[str, JsonValue],
    config: dict[str, Any],
    scratch: Path,
    isolation_mode: str = "unknown",
) -> Any:
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
    # Fixed before any gate runs: the gates check the change, they must not change it.
    # Nothing the harness (or a program its checkout made git run) left may outlive this.
    try:
        isolation.clear()
    except isolation.IsolationFailed as error:
        raise _uncontained(error) from None
    before = checkout_state(repo, env)
    bundle_sha256 = hashlib.sha256(bundle).hexdigest()

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

    # Before reporting: nothing the harness or a gate started is still running, and the
    # checkout and the bundle are what they were before the gates.
    try:
        isolation.clear()
    except isolation.IsolationFailed as error:
        raise _uncontained(error) from None
    try:
        after = checkout_state(repo, env)
    except (RuntimeError, OSError):
        after = {}
    if after != before or hashlib.sha256(bundle).hexdigest() != bundle_sha256:
        changed_keys = sorted(k for k in before if after.get(k) != before[k])
        detail = ", ".join(changed_keys) or "bundle"
        raise OperationRejected(
            f"a gate changed the checkout ({detail}); gates may check the change, not alter it",
            "gate_changed_checkout",
        )

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
        # How untrusted steps were contained: `subreaper` on Linux (cardea.hosts.isolation).
        "isolation": isolation_mode,
    }
    if not changed:
        return output
    return WithArtifacts(output=output, artifacts={BUNDLE: bundle})


async def run(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    """The confined worker's entry point (`coding.code.session`)."""
    # Before anything untrusted runs: same-uid code must not reach this process through
    # /proc, and a descendant that detaches must stay findable (cardea.hosts.isolation).
    try:
        mode = isolation.harden()
    except isolation.IsolationFailed as error:
        raise _uncontained(error) from None
    config = _config()
    # Before anything runs: the dependencies setting must agree with the profile's network.
    dependency_env(config)
    scratch = Path(tempfile.mkdtemp(prefix="code-session-"))
    try:
        return session(arguments, config, scratch, mode)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
