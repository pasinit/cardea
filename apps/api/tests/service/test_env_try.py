"""`env.try` (ADR-038): a commit's scenarios in a throwaway confined worker, the run window,
forced reruns and the receipt-result guard; portable, with a fake helper that runs the real
worker command as a local process."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cardea_env_ops import descriptors
from cardea_env_ops.try_ import scrub, stage
from test_code_session import document, kernel_for, receipt, submit, task

import cardea
from cardea.kernel.models import OperationState
from cardea.service import artifacts
from cardea.service.database import Fault
from cardea.service.kernel import Kernel, provision
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationRejected, Registry

PLANTED = {"GITHUB_TOKEN": "ghs_planted0000000000000000000000000000", "AWS_SECRET": "planted"}


class EnvHost:
    """The helper's interface for the `try` profile, running the worker locally."""

    def __init__(self, env: dict[str, str], root: Path, phases: bool = True) -> None:
        self.env = env
        self.root = root
        # Whether the host has the loopback phase: a workspace and the second profile.
        self.phases = phases
        self.outputs: dict[str, bytes] = {}
        self.started: list[str] = []
        # Each start as (profile, the job whose workspace it inherits).
        self.starts: list[tuple[str, str | None]] = []
        self.workspaces: dict[str, Path] = {}
        self.cleaned: list[str] = []

    def profiles(self) -> dict[str, Any]:
        profiles: dict[str, Any] = {
            "try": {
                "wall_seconds": 3600,
                "network": {"policy": "none"},
                "concurrency": 1,
                "sha256": "p-try",
            }
        }
        workers = {"env-try": ["try"]}
        if self.phases:
            profiles["try-loopback"] = {
                "wall_seconds": 3600,
                "network": {"policy": "loopback"},
                "concurrency": 1,
                "sha256": "p-loopback",
            }
            workers["env-try-loopback"] = ["try-loopback"]
        return {"profiles": profiles, "workers": workers, "sha256": "config"}

    def start_confined(
        self,
        job: str,
        content: bytes,
        deadline: float,
        profile: str,
        worker: str,
        workspace_from: str | None = None,
    ) -> None:
        self.started.append(job)
        self.starts.append((profile, workspace_from))
        extra = {}
        if profile == "try" and self.phases:
            self.workspaces[job] = self.root / f"workspace-{job[:8]}"
            self.workspaces[job].mkdir()
        if profile == "try-loopback":
            assert workspace_from in self.workspaces and workspace_from not in self.cleaned
            self.workspaces[job] = self.workspaces[workspace_from]
        if job in self.workspaces:
            extra["CARDEA_WORKSPACE"] = str(self.workspaces[job])
        done = subprocess.run(
            [sys.executable, "-m", "cardea.hosts.operation_worker", "env.try"],
            input=content,
            capture_output=True,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(cardea.__file__).parents[1]),
                **PLANTED,
                **self.env,
                **extra,
            },
            timeout=120,
            check=False,
        )
        self.outputs[job] = done.stdout

    def status(self, job: str) -> dict[str, Any]:
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "output": base64.b64encode(self.outputs[job]).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        return {}

    def cleanup(self, job: str) -> dict[str, Any]:
        self.cleaned.append(job)
        return {}


SCENARIO = """
import json, os, pathlib, sys
mode = pathlib.Path("mode").read_text().strip()
if sys.argv[1] == "fetch":
    pathlib.Path("fetched.txt").write_text("dependencies")
    sys.exit(0)
if sys.argv[1] == "fetch-fail":
    print("could not reach the package index")
    sys.exit(5)
if sys.argv[1] == "serve":
    # The fake host has no network namespace to prove; it checks the phases' hand-over.
    if not pathlib.Path("fetched.txt").exists():
        sys.exit(4)
    pathlib.Path("out").mkdir(exist_ok=True)
    pathlib.Path("out/serve.json").write_text(json.dumps({"reply": "pong"}))
    print("loopback ok")
    sys.exit(0)
print("scenario", sys.argv[1], "mode", mode)
print("token ghp_" + "a" * 30)
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/report.json").write_text(json.dumps({"mode": mode, "env": sorted(os.environ)}))
pathlib.Path("out/big.bin").write_bytes(b"x" * 300000)
sys.exit(0 if mode == "good" else 3)
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Any]:
    """A source with a good and a bad commit; the scenario reads `mode` from the tree."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    (work / "mode").write_text("good\n")
    git(work, "add", "mode")
    git(work, "commit", "--quiet", "-m", "good")
    good = git(work, "rev-parse", "HEAD")
    (work / "mode").write_text("bad\n")
    git(work, "commit", "--quiet", "-am", "bad")
    bad = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "source.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
    return {"source": bare, "good": good, "bad": bad}


def registry_file(tmp_path: Path, source: Path, **environment: Any) -> Path:
    script = tmp_path / "scenario.py"
    script.write_text(SCENARIO)
    catalog = {
        "scenarios": {
            "try": {
                "argv": [sys.executable, str(script), "try"],
                "timeout_seconds": 60,
                "artifacts": ["out/report.json", "out/big.bin", "missing.txt"],
            },
            "teams": {"argv": [sys.executable, str(script), "teams"], "timeout_seconds": 60},
            "boot": {"argv": ["/bin/true"], "tier": "init"},
            "serve": {
                "argv": [sys.executable, str(script), "serve"],
                "timeout_seconds": 60,
                "artifacts": ["out/serve.json"],
                "network": "loopback",
                "fetch": {"argv": [sys.executable, str(script), "fetch"], "timeout_seconds": 60},
            },
            "serve-offline": {
                "argv": [sys.executable, str(script), "serve"],
                "timeout_seconds": 60,
                "network": "loopback",
                "fetch": {"argv": [sys.executable, str(script), "fetch-fail"]},
            },
        },
        **environment,
    }
    path = tmp_path / "repositories.json"
    path.write_text(
        json.dumps({"repositories": {"app": {"source": str(source), "environment": catalog}}})
    )
    path.chmod(0o644)
    return path


@pytest.fixture
def env_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> tuple[Kernel, EnvHost]:
    registry = registry_file(tmp_path, repo["source"])
    # The kernel reads the registry for the run key; the worker reads it to run.
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(registry))
    host = EnvHost({"CARDEA_REPOSITORIES": str(registry)}, tmp_path)
    kernel = kernel_for(
        tmp_path, host, monkeypatch, Registry(descriptors(), {}), "env.try", ("record:work/attach",)
    )
    return kernel, host


def call(kernel: Kernel, key: str, **arguments: Any) -> tuple[str, Any]:
    task_id = submit(kernel, "env.try", {"repo": "app", **arguments}, key=key, grants=("repo:app",))
    state = OperationRuntime(kernel).execute(task_id)
    return state, receipt(kernel, task_id)


def output(settled: Any) -> dict[str, Any]:
    assert settled.state is OperationState.SUCCEEDED, settled.failure
    result: dict[str, Any] = settled.result["output"]
    return result


@pytest.mark.loopback
def test_a_passing_and_a_failing_commit(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = env_kernel
    _, good = call(kernel, "good", commit=repo["good"], scenarios=["try", "teams"])
    passed = output(good)
    assert passed["passed"] is True and passed["cached"] is False
    assert [(s["name"], s["outcome"], s["exit_code"]) for s in passed["scenarios"]] == [
        ("try", "pass", 0),
        ("teams", "pass", 0),
    ]
    first = passed["scenarios"][0]
    assert "scenario try mode good" in first["log_tail"]
    # Credential-shaped text is scrubbed from the log tail.
    assert "ghp_" not in first["log_tail"] and "[redacted]" in first["log_tail"]
    # One artifact kept; the large file and the missing path were dropped.
    assert [a["path"] for a in first["artifacts"]] == ["out/report.json"]
    assert passed["dropped"]["artifacts"] == 2
    (listed,) = passed["artifacts"]
    report = json.loads(artifacts.read(kernel.database, listed))
    assert report["mode"] == "good"
    # The scenario's environment was built from nothing: no planted credential.
    assert not {"GITHUB_TOKEN", "AWS_SECRET"} & set(report["env"])
    _, bad = call(kernel, "bad", commit=repo["bad"], scenarios=["try"])
    failed = output(bad)
    assert failed["passed"] is False
    assert (failed["scenarios"][0]["outcome"], failed["scenarios"][0]["exit_code"]) == ("fail", 3)


@pytest.mark.loopback
def test_the_same_inputs_return_the_same_result_without_running(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    _, first = call(kernel, "a", commit=repo["good"], scenarios=["try", "teams"])
    started = len(host.started)
    # The same scenarios in another order are the same run.
    _, again = call(kernel, "b", commit=repo["good"], scenarios=["teams", "try"])
    cached = output(again)
    assert len(host.started) == started
    assert cached["cached"] is True and cached["cached_receipt_id"] == first.receipt_id
    assert again.result.get("deduplicated") is True
    # Other scenarios, another commit, or a forced rerun run again.
    call(kernel, "c", commit=repo["good"], scenarios=["try"])
    call(kernel, "d", commit=repo["bad"], scenarios=["try", "teams"])
    _, forced = call(kernel, "e", commit=repo["good"], scenarios=["try", "teams"], rerun=True)
    assert len(host.started) == started + 3
    assert output(forced)["cached"] is False
    run = forced.request.arguments["run"]
    assert run["rerun_of"] == first.receipt_id


@pytest.mark.loopback
def test_a_changed_catalog_is_a_different_run(
    env_kernel: tuple[Kernel, EnvHost],
    repo: dict[str, Any],
    tmp_path: Path,
) -> None:
    kernel, host = env_kernel
    call(kernel, "a", commit=repo["good"], scenarios=["try"])
    started = len(host.started)
    registry_file(tmp_path, repo["source"], env={"FIXED": "1"})
    _, again = call(kernel, "b", commit=repo["good"], scenarios=["try"])
    assert output(again)["cached"] is False and len(host.started) == started + 1


@pytest.mark.loopback
def test_forced_reruns_are_limited_per_commit_and_day(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    for n in range(3):
        state, _ = call(kernel, f"r{n}", commit=repo["good"], scenarios=["try"], rerun=True)
        assert state == "succeeded"
    started = len(host.started)
    with pytest.raises(Fault) as refused:
        submit_and_run(kernel, "r3", commit=repo["good"], scenarios=["teams"], rerun=True)
    assert refused.value.code == "env_rerun_limit"
    assert len(host.started) == started
    # Another commit has its own allowance.
    assert call(kernel, "other", commit=repo["bad"], scenarios=["try"], rerun=True)[0] == (
        "succeeded"
    )


def submit_and_run(kernel: Kernel, key: str, **arguments: Any) -> str:
    """For refusals at admission, which the runtime reports as a failed task."""
    task_id = submit(kernel, "env.try", {"repo": "app", **arguments}, key=key, grants=("repo:app",))
    state = OperationRuntime(kernel).execute(task_id)
    if state == "failed":
        failure = task(kernel, task_id).failure
        raise Fault(failure.code, "refused", 409)
    return state


@pytest.mark.loopback
def test_an_identical_call_while_one_runs_is_refused(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    # Run once, then mark that run as still in progress.
    call(kernel, "first", commit=repo["good"], scenarios=["try"])
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE env_runs SET state='running',expires_at='2999-01-01T00:00:00+00:00'"
        )
    started = len(host.started)
    with pytest.raises(Fault) as refused:
        submit_and_run(kernel, "second", commit=repo["good"], scenarios=["try"])
    assert refused.value.code == "env_run_in_progress" and len(host.started) == started
    # A running row past its call's deadline no longer blocks.
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE env_runs SET expires_at='2000-01-01T00:00:00+00:00'")
    assert call(kernel, "third", commit=repo["good"], scenarios=["try"])[0] == "succeeded"


def test_a_head_outside_the_clone_refs_is_staged(tmp_path: Path, repo: dict[str, Any]) -> None:
    # A commit reachable only from a non-branch ref (a PR head), absent from a plain clone.
    work = tmp_path / "pr"
    git(tmp_path, "clone", "--quiet", str(repo["source"]), str(work))
    git(work, "config", "user.email", "a@example.com")
    git(work, "config", "user.name", "a")
    (work / "mode").write_text("pr\n")
    git(work, "commit", "--quiet", "-am", "pr")
    head = git(work, "rev-parse", "HEAD")
    git(work, "push", "--quiet", str(repo["source"]), f"{head}:refs/pull/1/head")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    checkout = stage(str(repo["source"]), head, scratch)
    assert (checkout / "mode").read_text() == "pr\n"
    missing = tmp_path / "scratch-unknown"
    missing.mkdir()
    with pytest.raises(OperationRejected) as unknown:
        stage(str(repo["source"]), "f" * 40, missing)
    assert unknown.value.code == "env_commit_unknown"


@pytest.mark.loopback
def test_a_failed_run_is_not_reused(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    state, refused = call(kernel, "x", commit="f" * 40, scenarios=["try"])
    assert state == "failed" and refused.failure.code == "env_commit_unknown"
    with kernel.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM env_runs").scalar_one() == 0


@pytest.mark.loopback
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"scenarios": ["nope"]}, "env_scenario_unknown"),
        ({"scenarios": ["boot"]}, "env_tier_unavailable"),
    ],
)
def test_unknown_scenarios_and_unavailable_tiers_are_refused(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any], arguments: Any, code: str
) -> None:
    kernel, _ = env_kernel
    state, refused = call(kernel, code, commit=repo["good"], **arguments)
    assert state == "failed" and refused.failure.code == code


@pytest.mark.parametrize(
    "arguments",
    [
        {"commit": "main", "scenarios": ["try"]},
        {"commit": "abc123", "scenarios": ["try"]},
        {"commit": "f" * 40, "scenarios": ["try"], "argv": ["sh"]},
        {"commit": "f" * 40, "scenarios": ["try"], "url": "https://example.org/x.git"},
    ],
)
def test_names_refs_and_command_lines_are_refused_before_anything_starts(
    env_kernel: tuple[Kernel, EnvHost], arguments: Any
) -> None:
    kernel, host = env_kernel
    with pytest.raises(Fault) as refused:
        submit(kernel, "env.try", {"repo": "app", **arguments}, grants=("repo:app",))
    assert refused.value.code == "invalid_arguments" and not host.started


def test_the_repository_grant_is_required(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    task_id = submit(
        kernel, "env.try", {"repo": "app", "commit": repo["good"], "scenarios": ["try"]}, grants=()
    )
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert not host.started


def test_scrub_hides_credential_shapes() -> None:
    text = "a ghs_" + "b" * 30 + " Bearer " + "c" * 20 + " AKIA" + "D" * 16 + " ok"
    cleaned = scrub(text)
    assert cleaned.count("[redacted]") == 3 and cleaned.endswith(" ok")


def test_scrub_keeps_task_ids() -> None:
    text = '"task_id": "task-' + "a1" * 16 + '"'
    assert scrub(text) == text
    assert scrub("key sk-" + "x" * 20) == "key [redacted]"


def test_the_descriptor_declares_the_run_window() -> None:
    (descriptor,) = descriptors()
    window = descriptor.cardea.run_window
    assert window is not None and window.seconds == 24 * 3600 and window.reruns_per_day == 3
    assert descriptor.cardea.effect == "read" and descriptor.cardea.resource is not None
    executor = descriptor.cardea.executor
    assert executor is not None and (executor.profile, executor.worker) == ("try", "env-try")


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "a/../../b"])
def test_artifact_paths_outside_the_checkout_are_refused_in_the_registry(path: str) -> None:
    from pydantic import ValidationError

    from cardea.service.repositories import Scenario

    with pytest.raises(ValidationError):
        Scenario.model_validate({"argv": ["/bin/true"], "artifacts": [path]})


@pytest.mark.loopback
def test_only_a_passing_run_satisfies_the_records_guard(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    """ADR-038 decision 3: `requires_receipt: {operation: env.try, result: ...}`."""
    from cardea.service.operations import OperationRejected
    from cardea.service.records import check_result

    kernel, _ = env_kernel
    _, good = call(kernel, "good", commit=repo["good"], scenarios=["try"])
    _, bad = call(kernel, "bad", commit=repo["bad"], scenarios=["try"])
    check_result("result.passed == true", good)
    with pytest.raises(OperationRejected) as refused:
        check_result("result.passed == true", bad)
    assert refused.value.code == "receipt_result_refused"
    # A cached answer carries the same verdict.
    _, cached = call(kernel, "again", commit=repo["good"], scenarios=["try"])
    check_result("result.passed == true && result.cached == true", cached)


# A hostile commit cannot change the reported result (ADR-038 review)

HOSTILE = r"""
import json, os, pathlib, signal, subprocess, sys, time
name = sys.argv[1]
worker = os.getppid()
if name == "forge":
    forged = json.dumps({"output": {"repo": "app", "commit": "0" * 40, "cached": False,
                                    "passed": True, "scenarios": []}}).encode()
    tried = {}
    for target in sorted(pathlib.Path(f"/proc/{worker}/fd").glob("*")) or [
        pathlib.Path(f"/proc/{worker}/fd/1")
    ]:
        try:
            with open(target, "wb") as stream:
                stream.write(forged)
            tried[target.name] = "written"
        except OSError as error:
            tried[target.name] = type(error).__name__
    try:
        with open(f"/proc/{worker}/mem", "rb") as stream:
            stream.read(1)
        tried["mem"] = "read"
    except OSError as error:
        tried["mem"] = type(error).__name__
    print("FORGE", json.dumps(tried, sort_keys=True))
    sys.exit(1)
if name == "background":
    # A detached process that would rewrite the next scenario's checkout and the report.
    code = (
        "import os, pathlib, time\n"
        "while True:\n"
        "    pathlib.Path('verdict').write_text('pass')\n"
        "    time.sleep(0.05)\n"
    )
    subprocess.Popen([sys.executable, "-c", code, "stray-marker"], start_new_session=True)
    time.sleep(0.5)
    sys.exit(0)
if name == "check":
    # Runs after `background`: the stray must be gone, so `verdict` stops changing.
    path = pathlib.Path("verdict")
    path.write_text("fail")
    time.sleep(0.5)
    print("VERDICT", path.read_text())
    sys.exit(0 if path.read_text() == "fail" else 1)
if name == "killer":
    os.kill(worker, signal.SIGKILL)
    time.sleep(1)
    sys.exit(0)
"""


@pytest.fixture
def hostile_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> tuple[Kernel, EnvHost]:
    script = tmp_path / "hostile.py"
    script.write_text(HOSTILE)
    scenarios = {
        name: {"argv": [sys.executable, str(script), name], "timeout_seconds": 30}
        for name in ("forge", "background", "check", "killer")
    }
    path = tmp_path / "repositories.json"
    path.write_text(
        json.dumps(
            {
                "repositories": {
                    "app": {"source": str(repo["source"]), "environment": {"scenarios": scenarios}}
                }
            }
        )
    )
    path.chmod(0o644)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(path))
    host = EnvHost({"CARDEA_REPOSITORIES": str(path)}, tmp_path)
    kernel = kernel_for(tmp_path, host, monkeypatch, Registry(descriptors(), {}), "env.try")
    return kernel, host


LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the isolation guards need Linux prctl and /proc"
)


@pytest.mark.loopback
@LINUX
def test_a_scenario_cannot_write_the_workers_result(
    hostile_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = hostile_kernel
    _, settled = call(kernel, "forge", commit=repo["good"], scenarios=["forge"])
    result = output(settled)
    assert result["passed"] is False and result["isolation"] == "subreaper"
    (scenario,) = result["scenarios"]
    assert scenario["outcome"] == "fail"
    line = next(x for x in scenario["log_tail"].splitlines() if x.startswith("FORGE "))
    tried = json.loads(line.removeprefix("FORGE "))
    assert tried and all(v not in {"written", "read"} for v in tried.values()), tried


@pytest.mark.loopback
@LINUX
def test_a_detached_background_process_does_not_outlive_its_scenario(
    hostile_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = hostile_kernel
    _, settled = call(kernel, "bg", commit=repo["good"], scenarios=["background", "check"])
    result = output(settled)
    background, check = result["scenarios"]
    assert background["stray_processes_killed"] >= 1
    assert check["outcome"] == "pass", check["log_tail"]
    assert result["passed"] is True
    # Nothing it started is still running anywhere.
    left = [
        p.name
        for p in Path("/proc").iterdir()
        if p.name.isdigit() and b"stray-marker" in _cmdline(p)
    ]
    assert not left


def _cmdline(path: Path) -> bytes:
    try:
        return (path / "cmdline").read_bytes()
    except OSError:
        return b""


@pytest.mark.loopback
@LINUX
def test_a_scenario_that_kills_the_worker_gets_no_result(
    hostile_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = hostile_kernel
    state, settled = call(kernel, "kill", commit=repo["good"], scenarios=["killer"])
    assert state == "failed" and settled.state is not OperationState.SUCCEEDED
    assert settled.result is None


# The loopback phase (ADR-038 amendment, 2026-11-22).
@pytest.mark.loopback
def test_a_loopback_scenario_runs_in_a_second_phase_after_its_fetch(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    _, settled = call(kernel, "loop", commit=repo["good"], scenarios=["serve", "try"])
    result = output(settled)
    assert result["passed"] is True and "next" not in result
    # The report is in the order the caller named, whichever phase ran each scenario.
    assert [(s["name"], s["outcome"]) for s in result["scenarios"]] == [
        ("serve", "pass"),
        ("try", "pass"),
    ]
    assert "loopback ok" in result["scenarios"][0]["log_tail"]
    # Two jobs: the networked one, then the loopback one on the first one's workspace.
    first = host.started[0]
    assert host.starts == [("try", None), ("try-loopback", first)]
    # The first phase's workspace outlives the second phase, and both are reaped after it.
    assert host.cleaned == [host.started[1], first]
    # Both phases' artifacts are stored, named apart.
    names = sorted(a["name"] for s in result["scenarios"] for a in s["artifacts"])
    assert names == ["a0", "a1"]
    kept = {a["path"]: a for s in result["scenarios"] for a in s["artifacts"]}
    assert set(kept) == {"out/report.json", "out/serve.json"}
    stored = {listed["name"]: listed for listed in result["artifacts"]}
    assert len(stored) == 2
    replies = [json.loads(artifacts.read(kernel.database, listed)) for listed in stored.values()]
    assert {"reply": "pong"} in replies
    # The same inputs are the same run: nothing starts again.
    call(kernel, "again", commit=repo["good"], scenarios=["try", "serve"])
    assert len(host.started) == 2


@pytest.mark.loopback
def test_a_failed_dependency_fetch_reports_the_scenario_without_a_second_phase(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    _, settled = call(kernel, "offline", commit=repo["good"], scenarios=["serve-offline", "try"])
    result = output(settled)
    assert result["passed"] is False
    by_name = {s["name"]: s for s in result["scenarios"]}
    assert by_name["serve-offline"]["outcome"] == "error"
    assert by_name["serve-offline"]["exit_code"] == 5
    assert "could not reach the package index" in by_name["serve-offline"]["log_tail"]
    assert by_name["try"]["outcome"] == "pass"
    # Nothing was left to serve, so no second unit started.
    assert host.starts == [("try", None)]


@pytest.mark.loopback
def test_other_scenarios_run_in_one_phase_as_before(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    _, settled = call(kernel, "plain", commit=repo["good"], scenarios=["try", "teams"])
    assert output(settled)["passed"] is True
    assert host.starts == [("try", None)]


@pytest.mark.loopback
def test_a_host_without_the_loopback_phase_refuses_a_loopback_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: dict[str, Any]
) -> None:
    registry = registry_file(tmp_path, repo["source"])
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(registry))
    host = EnvHost({"CARDEA_REPOSITORIES": str(registry)}, tmp_path, phases=False)
    kernel = kernel_for(tmp_path, host, monkeypatch, Registry(descriptors(), {}), "env.try")
    state, refused = call(kernel, "no-phase", commit=repo["good"], scenarios=["serve"])
    assert state == "failed" and refused.failure.code == "env_loopback_unavailable"
    # It never ran with the network the first phase has, and the checkout never happened.
    assert not list(tmp_path.glob("workspace-*"))
    # The host's other scenarios are unaffected, and the profile is not pinned.
    _, plain = call(kernel, "plain", commit=repo["good"], scenarios=["try"])
    assert output(plain)["passed"] is True
    assert "then" not in (kernel.executor_record("env.try") or {})


def test_the_second_phase_is_pinned_into_the_run_key(
    env_kernel: tuple[Kernel, EnvHost],
) -> None:
    kernel, _ = env_kernel
    record = kernel.executor_record("env.try")
    assert record is not None and record["then"]["profile_sha256"] == "p-loopback"  # type: ignore[index,call-overload]


def test_a_second_phase_that_does_not_match_the_first_is_refused(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any], tmp_path: Path
) -> None:
    from datetime import timedelta

    from cardea_env_ops.try_ import try_commit

    from cardea.service.models import now
    from cardea.service.operations import OperationContext, OperationRejected

    workspace = tmp_path / "ws"
    (workspace / "checkout").mkdir(parents=True)
    arguments: Any = {"repo": "app", "commit": repo["good"], "scenarios": ["serve"]}

    def context(previous: dict[str, Any]) -> OperationContext:
        return OperationContext(
            task_id="t",
            namespace="n",
            receipt_id="r",
            deadline=now() + timedelta(minutes=5),
            effect_key="r",
            previous=previous,
        )

    for previous in (
        {"next": ["serve"], "scenarios": [], "catalog_sha256": "0" * 64},  # another catalog
        {"next": ["try"], "scenarios": []},  # not a loopback scenario
        {"next": ["nope"], "scenarios": []},
    ):
        with pytest.raises(OperationRejected) as refused:
            try_commit(arguments, context(previous), tmp_path / "x", "subreaper", workspace)
        assert refused.value.code == "env_phase_invalid"
    with pytest.raises(OperationRejected) as missing:
        try_commit(arguments, context({"next": ["serve"]}), tmp_path / "x", "subreaper", None)
    assert missing.value.code == "env_phase_invalid"


# Attaching the result to a work item (record_id)

ITEM = "rec-" + "a" * 32
ATTACH = ("repo:app", "record:work/attach")


def item(kernel: Kernel) -> None:
    """A claimed work item in progress, inserted directly: only its links may change."""
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO collections(namespace,name,supervisor_id,generation,spec_json,"
            "spec_digest,created_at,updated_at) "
            "VALUES ('test','work','supervisor',1,'{}','x','t','t')"
        )
        conn.exec_driver_sql(
            "INSERT INTO records(namespace,collection,record_id,data_json,labels_json,links_json,"
            "state,holder,claim_epoch,lease_expires_at,version,created_by,created_at,updated_at) "
            "VALUES ('test','work',?,'{}','[]','[]','in_progress','dev-1',2,"
            "'2999-01-01T00:00:00.000000+00:00',1,'dev-1','2026-11-22T00:00:00',"
            "'2026-11-22T00:00:00')",
            (ITEM,),
        )


def stored(kernel: Kernel) -> dict[str, Any]:
    with kernel.database.read() as conn:
        row = conn.exec_driver_sql("SELECT * FROM records WHERE record_id=?", (ITEM,)).mappings()
        found = dict(row.one())
    found["links"] = json.loads(found["links_json"])
    return found


def attach_call(
    kernel: Kernel,
    key: str,
    grants: tuple[str, ...] = ATTACH,
    context: str | None = None,
    **arguments: Any,
) -> tuple[str, str]:
    """The task's state and, when it failed, the refusal's message."""
    task_id = submit(
        kernel,
        "env.try",
        {"repo": "app", "record_id": ITEM, **arguments},
        key=key,
        grants=grants,
        idempotency_context=context,
    )
    state = OperationRuntime(kernel).execute(task_id)
    failure = task(kernel, task_id).failure
    return state, failure.message if failure is not None else ""


@pytest.mark.loopback
def test_the_result_is_linked_to_the_item_without_touching_its_claim(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = env_kernel
    item(kernel)
    before = stored(kernel)
    assert attach_call(kernel, "ok", commit=repo["good"], scenarios=["try", "teams"])[0] == (
        "succeeded"
    )
    after = stored(kernel)
    (entry,) = after["links"]
    assert entry["rel"] == "env.try" and entry["observed_state"] == "pass"
    assert entry["url"].startswith(f"cardea://env.try/{repo['good']}?receipt=")
    assert len(repo["good"]) == 40 and entry["url"].endswith("&scenarios=try,teams")
    assert entry["external_id"] in entry["url"]
    # Holder, lease, claim epoch and state are the same; only the links (and version) moved.
    for column in ("holder", "lease_expires_at", "claim_epoch", "state", "data_json"):
        assert after[column] == before[column], column
    assert after["version"] == before["version"] + 1
    # A failing commit is linked as a failure.
    attach_call(kernel, "bad", commit=repo["bad"], scenarios=["try"])
    assert [link["observed_state"] for link in stored(kernel)["links"]] == ["pass", "fail"]


@pytest.mark.loopback
def test_a_role_without_the_grant_is_refused_naming_it_and_nothing_runs(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    item(kernel)
    state, message = attach_call(
        kernel, "denied", grants=("repo:app",), commit=repo["good"], scenarios=["try"]
    )
    assert state == "failed"
    assert "record:work/attach" in message
    assert host.started == [] and stored(kernel)["links"] == []
    # Without a record_id the call needs no such grant.
    assert call(kernel, "plain", commit=repo["good"], scenarios=["try"])[0] == "succeeded"


@pytest.mark.loopback
def test_authority_is_read_live_when_the_result_is_recorded(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, host = env_kernel
    item(kernel)
    real = host.start_confined

    def revoked(*args: Any) -> None:
        real(*args)
        # The supervisor loses the grant while the worker runs.
        provision(kernel.database, document("env.try"), Registry(descriptors(), {}))

    monkeypatch.setattr(host, "start_confined", revoked)
    state, message = attach_call(kernel, "live", commit=repo["good"], scenarios=["try"])
    assert state == "failed" and "record:work/attach" in message
    assert stored(kernel)["links"] == []


@pytest.mark.loopback
def test_a_repeated_idempotency_key_adds_one_entry(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, _ = env_kernel
    item(kernel)
    # Even a forced rerun with the same key is one entry; the key names it.
    for key, rerun in (("first", False), ("second", True)):
        state, _ = attach_call(
            kernel, key, context="one-key", commit=repo["good"], scenarios=["try"], rerun=rerun
        )
        assert state == "succeeded"
    (entry,) = stored(kernel)["links"]
    assert entry["external_id"] == "one-key"


@pytest.mark.loopback
def test_a_cached_result_is_linked_to_another_item_by_its_own_receipt(
    env_kernel: tuple[Kernel, EnvHost], repo: dict[str, Any]
) -> None:
    kernel, host = env_kernel
    item(kernel)
    _, first = call(kernel, "a", commit=repo["good"], scenarios=["try"])
    started = len(host.started)
    assert attach_call(kernel, "b", commit=repo["good"], scenarios=["try"])[0] == "succeeded"
    assert len(host.started) == started  # answered from the window, still linked
    (entry,) = stored(kernel)["links"]
    assert entry["external_id"] == first.receipt_id
    # The same call again adds nothing.
    attach_call(kernel, "c", commit=repo["good"], scenarios=["try"])
    assert len(stored(kernel)["links"]) == 1
