"""`env.try` inside its confined worker (ADR-038), the process tier.

One call, nothing kept afterwards:

1. **Stage.** Clone the repository named by its alias from the host owner's read-only
   source (the repository registry's `source`) into the job's scratch, with every git hook
   and global configuration off, and check out exactly `commit`, a full object ID. The
   checked-out HEAD must equal it.
2. **Run.** Each named scenario from the repository's `environment` catalog, in order, in
   the checkout: its fixed argv, its own time limit, all under the call's deadline. Commands
   get an environment built from nothing: the pinned toolchain image first on `PATH`, a
   scratch `HOME`, the profile's egress proxy, and the catalog's fixed variables.
3. **Collect.** Per scenario: `pass`, `fail`, `timeout`, `error` (it could not start) or
   `not_run` (the deadline passed first); the exit status and duration; a scrubbed log tail
   of at most 4 KiB; and the catalog's artifact paths, each at most 256 KiB and 1 MiB in
   all, returned as artifacts the kernel stores as objects. Anything over a limit is
   dropped and counted.
4. **Isolation.** The worker is non-dumpable and a child subreaper, each scenario runs in
   its own session, and every process it started is killed before the next scenario and
   before the report (`cardea.hosts.isolation`, shared with `code.session`), so the commit
   cannot forge or disturb the result.
5. **Tear down.** The scratch is deleted here; the root helper removes the unit and its
   staging whatever happens (ADR-028).

The run window (the same inputs within 24 hours give the same receipt) and the rerun limit
are the kernel's (`cardea.service.env_runs`); this worker only runs.

A caller names an alias, a commit and scenario names, never a command, a path or a URL. A
scenario that needs the `init` tier is refused (`env_tier_unavailable`): this worker is the
process tier, and it never runs a scenario in a weaker tier than it asks for.

**Loopback scenarios.** A scenario that needs TCP on a loopback (`make teams`, an MCP
loopback test) declares `network: loopback`. The unit this worker runs in reaches the egress
proxy and nothing else, and the host's loopback not at all, so it cannot serve such a
scenario. It runs in a second phase, a second worker under a profile whose unit has a private
network namespace with only its own loopback, no proxy and no route out:

1. *First phase* (this worker, the `try` profile): everything above for the scenarios that
   do not need loopback, plus each loopback scenario's `fetch`, its dependency fetch through
   the proxy. All of it happens in the job's workspace (`$CARDEA_WORKSPACE`), which the second
   phase inherits. A failed fetch reports that scenario as `error` and it is not run. What is
   left is reported as `next`, and the kernel starts the second phase.
2. *Second phase* (`phase_two`, the `try-loopback` profile): the deferred scenarios in the
   same checkout, then the merged report in the order the caller named them. It has the
   kernel's copy of the first phase's output (`context.previous`), never a file the commit
   could have written.

A host without the second phase refuses the call before running anything
(`env_loopback_unavailable`), never running a loopback scenario with the network it has.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from cardea.hosts import isolation
from cardea.runtime.harness import PROXY_VARIABLES
from cardea.service import repositories
from cardea.service.operations import OperationContext, OperationRejected, WithArtifacts

TIER = "process"
# The refusal when a scenario's processes cannot be contained.
CODE = "env_isolation_failed"
MAX_TAIL = 4096
MAX_ARTIFACT = 256 * 1024
MAX_ARTIFACTS_TOTAL = 1024 * 1024
MAX_ARTIFACT_FILES = 8
COMMIT = re.compile(r"^[0-9a-f]{40}$")
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
PROXY = PROXY_VARIABLES
# Never set for a scenario: anything that could carry a credential.
CREDENTIAL_NAMES = re.compile(
    r"^(GH_|GITHUB_|GIT_|GITLAB_|GITEA_|AWS_|GOOGLE_|AZURE_)|TOKEN|SECRET|PASSWORD|"
    r"ASKPASS|CREDENTIAL|^SSH_AUTH_SOCK$|^NETRC$|API_KEY"
)
# Credential-shaped strings scrubbed from log tails (ADR-026's summary rule).
SECRET_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|\bsk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:bearer)\s+[A-Za-z0-9._~+/-]{16,}=*)"
)


def scrub(text: str) -> str:
    return SECRET_SHAPES.sub("[redacted]", text)


def _git(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def environment(scratch: Path, catalog: repositories.Environment) -> dict[str, str]:
    """A scenario's whole environment, built from nothing."""
    home = scratch / "home"
    home.mkdir(exist_ok=True)
    path = BASE_PATH
    if catalog.toolchain is not None:
        root = catalog.toolchain.path.rstrip("/")
        path = f"{root}/usr/local/bin:{root}/usr/bin:{root}/bin:{BASE_PATH}"
    env = {
        "PATH": path,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(scratch / "tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "CI": "1",
    }
    (scratch / "tmp").mkdir(exist_ok=True)
    for name in PROXY:
        if name in os.environ:
            env[name] = os.environ[name]
    for name, value in catalog.env.items():
        if CREDENTIAL_NAMES.search(name):
            raise OperationRejected(f"the scenario environment may not set {name}", "env_invalid")
        env[name] = value
    return env


def stage(source: str, commit: str, scratch: Path) -> Path:
    """A checkout of exactly `commit`, cloned from the read-only source."""
    env = {"PATH": BASE_PATH, "HOME": str(scratch), "GIT_CONFIG_NOSYSTEM": "1"}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    checkout = scratch / "checkout"
    cloned = _git(scratch, env, "clone", "--quiet", "--no-checkout", source, str(checkout))
    if cloned.returncode:
        raise OperationRejected("the repository source could not be read", "env_source_unavailable")
    switched = _git(checkout, env, "checkout", "--quiet", "--detach", commit)
    if switched.returncode:
        raise OperationRejected("the commit is not in the repository", "env_commit_unknown")
    head = _git(checkout, env, "rev-parse", "HEAD").stdout.strip()
    if head != commit:
        raise OperationRejected("the checkout is not the named commit", "env_commit_unknown")
    return checkout


def _run(argv: tuple[str, ...], cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    """One scenario in its own session, through the shared isolation helper: afterwards
    nothing it started is left running (or the call fails with `env_isolation_failed`)."""
    try:
        step = isolation.run_step(list(argv), cwd, env, timeout, code=CODE)
    except isolation.IsolationFailed as failed:
        raise OperationRejected(str(failed), failed.code) from None
    if step.timed_out:
        outcome = "timeout"
    elif step.returncode is None:
        outcome = "error"  # it could not start
    else:
        outcome = "pass" if step.returncode == 0 else "fail"
    text = step.stdout + step.stderr
    return {
        "outcome": outcome,
        "exit_code": step.returncode,
        "duration_ms": step.duration_ms,
        "log_tail": scrub(text[-MAX_TAIL:]),
        "log_truncated": len(text) > MAX_TAIL,
        "stray_processes_killed": step.killed,
        "dropped_log_bytes": max(0, len(text.encode()) - MAX_TAIL),
    }


def collect(
    checkout: Path,
    paths: tuple[str, ...],
    kept: dict[str, bytes],
    dropped: dict[str, int],
    earlier: tuple[int, int] = (0, 0),
) -> list[JsonValue]:
    """The scenario's artifact paths, within the per-file, total and count limits.

    `earlier` is the count and size of the artifacts an earlier phase of the call kept."""
    listed: list[JsonValue] = []
    root = checkout.resolve()
    for relative in paths:
        target = (checkout / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            dropped["artifacts"] += 1
            continue
        data = target.read_bytes()
        total = earlier[1] + sum(len(v) for v in kept.values()) + len(data)
        if (
            len(data) > MAX_ARTIFACT
            or total > MAX_ARTIFACTS_TOTAL
            or earlier[0] + len(kept) >= MAX_ARTIFACT_FILES
        ):
            dropped["artifacts"] += 1
            continue
        name = f"a{earlier[0] + len(kept)}"
        kept[name] = data
        listed.append({"name": name, "path": relative, "bytes": len(data)})
    return listed


def _admit(arguments: dict[str, JsonValue]) -> tuple[str, str, list[str], Any, Any]:
    """The call's alias, commit, scenario names, repository entry and catalog, or a refusal."""
    alias, commit = str(arguments["repo"]), str(arguments["commit"])
    names = [str(n) for n in arguments["scenarios"]]  # type: ignore[union-attr]
    if not COMMIT.fullmatch(commit):
        raise OperationRejected("commit must be a full 40-hex object ID", "invalid_arguments")
    registry = repositories.load()
    entry = registry.repositories.get(alias) if registry is not None else None
    if entry is None:
        raise OperationRejected("No such repository alias on this host", "repository_unknown")
    catalog = entry.environment
    if catalog is None:
        raise OperationRejected("env.try is not configured for this repository", "env_unavailable")
    unknown = [n for n in names if n not in catalog.scenarios]
    if unknown:
        raise OperationRejected(f"Unknown scenarios: {', '.join(unknown)}", "env_scenario_unknown")
    for name in names:
        tier = catalog.scenarios[name].tier
        if tier != TIER or tier not in catalog.tiers:
            raise OperationRejected(
                f"Scenario {name} needs the {tier} tier, which this host does not offer",
                "env_tier_unavailable",
            )
    if catalog.toolchain is not None and not Path(catalog.toolchain.path).is_dir():
        raise OperationRejected("The pinned toolchain image is missing", "env_toolchain_missing")
    return alias, commit, names, entry, catalog


def _scenario(
    name: str,
    argv: tuple[str, ...],
    timeout_seconds: int,
    artifacts: tuple[str, ...],
    checkout: Path,
    env: dict[str, str],
    deadline: float,
    kept: dict[str, bytes],
    dropped: dict[str, int],
    earlier: tuple[int, int] = (0, 0),
) -> dict[str, JsonValue]:
    """One scenario's report, `not_run` when the call's deadline has passed."""
    remaining = deadline - time.time()
    if remaining <= 1:
        return {"name": name, "outcome": "not_run", "exit_code": None}
    ran = _run(argv, checkout, env, min(float(timeout_seconds), remaining))
    dropped["log_bytes"] += int(ran.pop("dropped_log_bytes"))
    ran["artifacts"] = collect(checkout, artifacts, kept, dropped, earlier)
    return {"name": name, **ran}


def _report(
    alias: str,
    commit: str,
    catalog: repositories.Environment,
    mode: str,
    results: list[JsonValue],
    dropped: dict[str, int],
) -> dict[str, JsonValue]:
    passed = all(isinstance(r, dict) and r.get("outcome") == "pass" for r in results)
    return {
        "repo": alias,
        "commit": commit,
        "tier": TIER,
        "isolation": mode,
        "catalog_sha256": catalog.digest(),
        "toolchain_sha256": catalog.toolchain.sha256 if catalog.toolchain else None,
        "cached": False,
        "passed": passed,
        "scenarios": results,
        "dropped": dropped,  # type: ignore[dict-item]
    }


def try_commit(
    arguments: dict[str, JsonValue],
    context: OperationContext,
    scratch: Path,
    mode: str = "subreaper",
    workspace: Path | None = None,
) -> WithArtifacts | dict[str, JsonValue]:
    if context.previous is not None:
        if workspace is None:
            raise OperationRejected("The second phase has no workspace", "env_phase_invalid")
        return phase_two(arguments, context, workspace, mode)
    alias, commit, names, entry, catalog = _admit(arguments)
    deferred = [n for n in names if catalog.scenarios[n].network == "loopback"]
    if deferred:
        if workspace is None:
            raise OperationRejected(
                f"Scenarios {', '.join(deferred)} need loopback, whose second phase this host "
                "does not offer",
                "env_loopback_unavailable",
            )
        scratch = workspace  # the second phase inherits it, checkout and fetched dependencies
    env = environment(scratch, catalog)
    checkout = stage(entry.source, commit, scratch)
    deadline = context.deadline.timestamp() - 5  # leave time to report
    dropped = {"artifacts": 0, "log_bytes": 0}
    kept: dict[str, bytes] = {}
    results: dict[str, JsonValue] = {}
    for name in names:
        if name in deferred:
            continue
        scenario = catalog.scenarios[name]
        results[name] = _scenario(
            name, scenario.argv, scenario.timeout_seconds, scenario.artifacts,
            checkout, env, deadline, kept, dropped,
        )  # fmt: skip
    # The dependency fetch of each loopback scenario, through the proxy, before its phase.
    ready: list[JsonValue] = []
    fetched: dict[tuple[str, ...], dict[str, JsonValue]] = {}
    for name in deferred:
        fetch = catalog.scenarios[name].fetch
        if fetch is None:
            ready.append(name)
            continue
        if fetch.argv not in fetched:
            fetched[fetch.argv] = _scenario(
                name, fetch.argv, fetch.timeout_seconds, (), checkout, env, deadline, {}, dropped
            )
        done = fetched[fetch.argv]
        if done["outcome"] == "pass":
            ready.append(name)
        else:
            # It could not start without its dependencies; the fetch's own tail says why.
            results[name] = {**done, "name": name, "outcome": "error", "artifacts": []}
    # Nothing the commit started may outlive its scenarios into the report.
    try:
        isolation.clear(code=CODE)
    except isolation.IsolationFailed as failed:
        raise OperationRejected(str(failed), failed.code) from None
    listed: list[JsonValue] = [results[n] for n in names if n in results]
    output = _report(alias, commit, catalog, mode, listed, dropped)
    if ready:
        output["next"] = ready
        del output["passed"]
    return WithArtifacts(output, kept) if kept else output


def _kept(reported: list[JsonValue]) -> tuple[int, int]:
    """The count and size of the artifacts the first phase kept, from its report."""
    count = size = 0
    for result in reported:
        listed = result.get("artifacts") if isinstance(result, dict) else None
        for artifact in listed if isinstance(listed, list) else []:
            count += 1
            amount = artifact.get("bytes") if isinstance(artifact, dict) else None
            size += amount if isinstance(amount, int) else 0
    return count, size


def phase_two(
    arguments: dict[str, JsonValue], context: OperationContext, workspace: Path, mode: str
) -> WithArtifacts | dict[str, JsonValue]:
    """The loopback scenarios, in the checkout the first phase left in the workspace."""
    alias, commit, names, _, catalog = _admit(arguments)
    previous = dict(context.previous or {})
    ready = previous.get("next")
    reported = previous.get("scenarios")
    checkout = workspace / "checkout"
    if (
        not isinstance(ready, list)
        or not isinstance(reported, list)
        or not all(isinstance(n, str) and n in names for n in ready)
        or any(catalog.scenarios[str(n)].network != "loopback" for n in ready)
        or previous.get("catalog_sha256") != catalog.digest()
        or not checkout.is_dir()
    ):
        raise OperationRejected("The second phase does not match the first", "env_phase_invalid")
    env = environment(workspace, catalog)
    results: dict[str, JsonValue] = {
        str(r["name"]): r for r in reported if isinstance(r, dict) and "name" in r
    }
    earlier = _kept(reported)
    first_dropped = previous.get("dropped")
    dropped = {"artifacts": 0, "log_bytes": 0}
    if isinstance(first_dropped, dict):
        for key in dropped:
            value = first_dropped.get(key)
            dropped[key] = value if isinstance(value, int) else 0
    deadline = context.deadline.timestamp() - 5
    kept: dict[str, bytes] = {}
    for name in names:
        if name not in ready:
            continue
        scenario = catalog.scenarios[name]
        results[name] = _scenario(
            name, scenario.argv, scenario.timeout_seconds, scenario.artifacts,
            checkout, env, deadline, kept, dropped, earlier,
        )  # fmt: skip
    try:
        isolation.clear(code=CODE)
    except isolation.IsolationFailed as failed:
        raise OperationRejected(str(failed), failed.code) from None
    if any(n not in results for n in names):
        raise OperationRejected("The second phase does not match the first", "env_phase_invalid")
    output = _report(alias, commit, catalog, mode, [results[n] for n in names], dropped)
    return WithArtifacts(output, kept) if kept else output


async def run(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    """The confined worker's entry point (`env.try`), in either phase."""
    # Before anything is staged: the commit's code must not reach this process.
    try:
        mode = isolation.harden(code=CODE)
    except isolation.IsolationFailed as failed:
        raise OperationRejected(str(failed), failed.code) from None
    folder = os.environ.get("CARDEA_WORKSPACE")
    workspace = Path(folder) if folder else None
    scratch = Path(tempfile.mkdtemp(prefix="env-try-"))
    try:
        return try_commit(arguments, context, scratch, mode, workspace)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
