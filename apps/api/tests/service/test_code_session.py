"""Long confined operations (ADR-023 amendment) and `code.session` with a fake harness and
fake gates (ADR-037 slices 2 and 3); portable, with a fake helper that runs the real worker
command as a local process."""

import base64
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_operations import Auth, FakeClient

import cardea
from cardea.kernel.models import OperationState, TaskState
from cardea.service import artifacts, confined, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.auth import Authentication
from cardea.service.confined import Workers
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    OperationTaskRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import OperationDescriptor, Registry, WithArtifacts

DESCRIPTOR = Path(__file__).parents[4] / (
    "packages/cardea-coding-ops/src/cardea_coding_ops/descriptors/code.session.json"
)
# Credentials a careless host might leave in the worker's environment.
PLANTED = {
    "GITHUB_TOKEN": "ghs_planted0000000000000000000000000000",
    "GH_TOKEN": "gho_planted1111111111111111111111111111",
    "GIT_ASKPASS": "/planted/askpass",
    "GIT_CONFIG_PARAMETERS": "'credential.helper=planted'",
}


class LocalHost:
    """The helper's interface, running the worker command as a plain local process."""

    def __init__(self, profiles: dict[str, Any], env: dict[str, str] | None = None) -> None:
        self._profiles = profiles
        self.env = env or {}
        self.outputs: dict[str, bytes] = {}
        self.stopped: list[str] = []

    def profiles(self) -> dict[str, Any]:
        return self._profiles

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "cardea.hosts.operation_worker", "coding.code.session"],
            input=content,
            capture_output=True,
            # The kernel source is on the test's path, not installed in the environment.
            env={**os.environ, "PYTHONPATH": str(Path(cardea.__file__).parents[1]), **self.env},
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
        self.stopped.append(job)
        return {}

    def cleanup(self, job: str) -> dict[str, Any]:
        return {}


def profiles(wall_seconds: int = 3600) -> dict[str, Any]:
    return {
        "profiles": {
            "coding": {
                "wall_seconds": wall_seconds,
                "network": {"policy": "model_api_only"},
                "sha256": "p-coding",
            }
        },
        "workers": {"code-session": ["coding"]},
        "sha256": "config",
    }


def descriptor(**binding: Any) -> OperationDescriptor:
    document = json.loads(DESCRIPTOR.read_text())
    document["cardea"].update(binding)
    return OperationDescriptor.model_validate(document)


def kernel_for(
    tmp_path: Path,
    host: Any,
    monkeypatch: pytest.MonkeyPatch,
    registry: Registry,
    operation: str = "code.session",
    extra: tuple[str, ...] = (),
) -> Kernel:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.02)
    # Short heartbeat periods, so a lost lease shows within a test's time.
    monkeypatch.setattr(operation_runtime, "HEARTBEAT_LEASE_SECONDS", 1.0)
    monkeypatch.setattr(operation_runtime, "HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(operation, extra), registry)
    return Kernel(db, cast(Authentication, Auth()), operations=registry, workers=Workers(host))


def document(operation: str, extra: tuple[str, ...] = ()) -> Provisioning:
    """The supervisor's and the platform's authority for one operation and its extras."""
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*RIGHTS, operation, "repo:app", *extra)
    )
    principals = tuple(
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="test",
            subject=p,
        )
        for p in ("runtime", "supervisor", "outsider")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    return Provisioning(
        principals=principals,
        permissions=granted,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
    )


def submit(
    kernel: Kernel,
    operation: str,
    arguments: dict[str, Any],
    key: str = "one",
    grants: tuple[str, ...] | None = None,
    idempotency_context: str | None = None,
) -> str:
    if grants is None:
        # `code.session` needs the repository's `repo:<alias>` grant (ADR-037).
        repository = arguments.get("repository")
        grants = (f"repo:{repository}",) if isinstance(repository, str) else ()
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"call-{key}",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "One long call",
            "idempotency_context": idempotency_context,
            "operation": operation,
            "arguments": arguments,
            "grants": ["objects.read", "objects.write", operation, *grants],
            "timeout_seconds": 3600,
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


def receipt(kernel: Kernel, task_id: str) -> Any:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_operation(task_id, "execute")


def task(kernel: Kernel, task_id: str) -> Any:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_task(task_id)


# code.session: fixtures

HARNESS = """
import json, os, pathlib
script = json.loads(os.environ["CARDEA_TASK"])
for path, content in script.get("write", {}).items():
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
# What the harness could see, kept in the change so the test can inspect it.
pathlib.Path("env-seen.json").write_text(json.dumps(dict(os.environ), sort_keys=True))
print("applied", len(script.get("write", {})), "files")
"""

GATE = """
import json, os, sys
print(json.dumps(dict(os.environ), sort_keys=True))
sys.exit(int(sys.argv[1]))
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A read-only repository source: a bare repository with one commit on `main`."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch=main")
    (work / "README").write_text("fixture\n")
    git(work, "add", "README")
    git(work, "commit", "--quiet", "-m", "initial")
    bare = tmp_path / "source.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(work), str(bare))
    return bare


def coding_config(
    tmp_path: Path, source: Path, gate_exit: int = 0, legacy: bool = False, **harness: Any
) -> Path:
    """`code.session`'s own file (the harness) and the host's repository registry beside it
    (`repositories.json`, ADR-037). `legacy` puts the repositories in the coding file
    instead, the deprecated layout."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "harness.py").write_text(HARNESS)
    (scripts / "gate.py").write_text(GATE)
    gates = [
        {"name": "lint", "argv": [sys.executable, str(scripts / "gate.py"), "0"]},
        {"name": "test", "argv": [sys.executable, str(scripts / "gate.py"), str(gate_exit)]},
    ]
    entries = {"app": {"source": str(source), "gates": gates}}
    config: dict[str, Any] = {
        "harness": {
            "argv": [sys.executable, str(scripts / "harness.py")],
            "passthrough": ["MODEL_API_KEY"],
            **harness,
        },
    }
    if legacy:
        config["repositories"] = entries
    path = tmp_path / "coding.json"
    path.write_text(json.dumps(config))
    registry = tmp_path / "repositories.json"
    registry.write_text(json.dumps({"repositories": entries}))
    registry.chmod(0o600)
    return path


def coding_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    gate_exit: int = 0,
    wall_seconds: int = 3600,
    **binding: Any,
) -> tuple[Kernel, LocalHost]:
    config = coding_config(tmp_path, source, gate_exit)
    host = LocalHost(
        profiles(wall_seconds),
        {
            "CARDEA_CODING_CONFIG": str(config),
            "CARDEA_REPOSITORIES": str(tmp_path / "repositories.json"),
            "MODEL_API_KEY": "model-key",
            **PLANTED,
        },
    )
    registry = Registry((descriptor(**binding),), {})
    return kernel_for(tmp_path, host, monkeypatch, registry), host


def change(edits: dict[str, str]) -> dict[str, Any]:
    return {
        "repository": "app",
        "base_ref": "main",
        "task": json.dumps({"write": edits}),
        "task_key": "issue-1",
    }


# code.session


def test_a_session_returns_a_bounded_receipt_and_the_change_as_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    task_id = submit(kernel, "code.session", change({"src/app.py": "print('hello')\n"}))
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    output = receipt(kernel, task_id).result["output"]
    assert output["changed"] is True and output["files_changed"] == 2  # app.py, env-seen.json
    assert output["head_sha"] != output["base_sha"]
    assert "src/app.py" in output["diff_excerpt"]
    assert [(g["name"], g["passed"]) for g in output["gates"]] == [("lint", True), ("test", True)]
    assert output["gates_passed"] is True
    assert output["harness"]["exit_code"] == 0
    (bundle,) = output["artifacts"]
    assert bundle["name"] == "change.bundle" and bundle["parts"]
    # The bundle is the commits on top of base_ref; it applies to a fresh clone.
    content = artifacts.read(kernel.database, bundle)
    (tmp_path / "change.bundle").write_bytes(content)
    check = tmp_path / "check"
    git(tmp_path, "clone", "--quiet", str(source), str(check))
    git(check, "bundle", "verify", str(tmp_path / "change.bundle"))
    git(check, "fetch", "--quiet", str(tmp_path / "change.bundle"), "HEAD:refs/heads/change")
    assert git(check, "show", "change:src/app.py") == "print('hello')\n"


def test_failing_gates_are_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source, gate_exit=3)
    task_id = submit(kernel, "code.session", change({"x.txt": "x\n"}))
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    output = receipt(kernel, task_id).result["output"]
    assert [(g["name"], g["exit_code"]) for g in output["gates"]] == [("lint", 0), ("test", 3)]
    assert output["gates_passed"] is False


def test_a_call_without_the_repo_grant_is_refused_before_a_worker_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    """`code.session` needs `repo:<alias>` like `vcs.*` (ADR-037, rehearsal finding 2): the
    kernel refuses the call from the descriptor's `resource` before any intent exists."""
    kernel, host = coding_kernel(tmp_path, monkeypatch, source)
    started: list[str] = []
    real = host.start_confined

    def recording(job: str, *args: Any) -> None:
        started.append(job)
        real(job, *args)

    host.start_confined = recording  # type: ignore[method-assign,assignment]
    task_id = submit(kernel, "code.session", change({"x.txt": "x\n"}), grants=())
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    assert cast(Any, task(kernel, task_id).failure).code == "task_grant_denied"
    with kernel.database.read() as conn, borrow(conn) as tx:
        assert tx.list_operations(task_id) == []
    assert started == []
    granted = submit(kernel, "code.session", change({"x.txt": "x\n"}), key="two")
    assert OperationRuntime(kernel).execute(granted) == "succeeded" and len(started) == 1


def test_no_repository_host_credential_reaches_the_harness_or_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}))
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    output = receipt(kernel, task_id).result["output"]
    content = artifacts.read(kernel.database, output["artifacts"][0])
    (tmp_path / "c.bundle").write_bytes(content)
    check = tmp_path / "check"
    git(tmp_path, "clone", "--quiet", str(source), str(check))
    git(check, "fetch", "--quiet", str(tmp_path / "c.bundle"), "HEAD:refs/heads/c")
    seen = json.loads(git(check, "show", "c:env-seen.json"))
    gate_env = json.loads(output["gates"][0]["output_tail"])
    for env in (seen, gate_env):
        assert not set(PLANTED) & set(env)
        assert not {k for k in env if k.startswith(("GH_", "GITHUB_"))}
        assert not any(value in json.dumps(env) for value in PLANTED.values())
    # The harness gets the model credential it is configured to receive; gates do not.
    assert seen["MODEL_API_KEY"] == "model-key" and "MODEL_API_KEY" not in gate_env
    # Nothing the receipt or the stored output holds carries a planted value either.
    recorded = json.dumps(receipt(kernel, task_id).model_dump(mode="json"))
    assert not any(value in recorded for value in PLANTED.values())


def test_a_harness_may_not_be_configured_to_receive_a_host_credential(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardea_coding_ops.session import session

    from cardea.service.operations import OperationRejected

    config = json.loads(coding_config(tmp_path, source).read_text())
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(tmp_path / "repositories.json"))
    config["harness"]["passthrough"] = ["GITHUB_TOKEN"]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(OperationRejected, match="GITHUB_TOKEN"):
        session(change({"a": "a"}), config, scratch)
    with pytest.raises(OperationRejected, match="not allowed"):
        session({**change({}), "repository": "other"}, config, scratch)


def test_repositories_come_from_the_host_registry(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One registry (ADR-037 finding 3): the coding file's own `repositories` still work for
    one release with a warning, and a coding file that disagrees with the registry is
    refused before anything is cloned."""
    from cardea_coding_ops.session import repository_entry

    from cardea.service.operations import OperationRejected

    config = json.loads(coding_config(tmp_path, source, legacy=True).read_text())
    with pytest.warns(DeprecationWarning, match="CARDEA_CODING_CONFIG"):
        legacy = repository_entry("app", config)
    assert legacy is not None and legacy["source"] == str(source)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(tmp_path / "repositories.json"))
    entry = repository_entry("app", {"harness": config["harness"]})
    assert entry is not None and [g["name"] for g in entry["gates"]] == ["lint", "test"]
    with pytest.warns(DeprecationWarning):
        assert repository_entry("app", config) == entry  # agreeing: the registry's
    config["repositories"]["app"]["source"] = "/elsewhere.git"
    with pytest.warns(DeprecationWarning), pytest.raises(OperationRejected) as refused:
        repository_entry("app", config)
    assert refused.value.code == "repositories_disagree"


def test_an_oversized_change_is_refused_and_reconciles_as_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source, max_artifact_bytes=256)
    task_id = submit(kernel, "code.session", change({"big.txt": os.urandom(2048).hex()}))
    OperationRuntime(kernel).execute(task_id)
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN
    with kernel.database.read() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM objects WHERE resource_id LIKE 'receipt-%'"
            ).scalar_one()
            == 0
        )
    assert OperationRuntime(kernel).reconcile(task_id, "execute") == "failed"


def test_a_timeout_beyond_the_profile_wall_time_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source, wall_seconds=600)
    with pytest.raises(Fault) as refused:
        submit(kernel, "code.session", change({"a": "a"}))
    assert refused.value.code == "operation_timeout_exceeds_profile"


@pytest.mark.parametrize("timeout", [601, 7201])
def test_descriptor_timeouts_have_hard_limits(timeout: int) -> None:
    from pydantic import ValidationError

    if timeout > 7200:
        with pytest.raises(ValidationError):
            descriptor(timeout_seconds=timeout)
    else:
        assert descriptor(timeout_seconds=timeout).cardea.timeout_seconds == timeout
        document = json.loads(DESCRIPTOR.read_text())
        document["cardea"] = {"version": 1, "effect": "read", "handler": "x.y"}
        document["cardea"]["timeout_seconds"] = timeout
        with pytest.raises(ValidationError, match="only confined"):
            OperationDescriptor.model_validate(document)


# Long confined calls: heartbeat lease and fencing


class SlowHost(LocalHost):
    """A worker that runs for `seconds` and then finishes with an empty output."""

    def __init__(self, seconds: float) -> None:
        super().__init__(profiles(7200))
        self.seconds = seconds
        self.started: dict[str, float] = {}

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        self.started[job] = time.monotonic()

    def status(self, job: str) -> dict[str, Any]:
        if time.monotonic() - self.started[job] < self.seconds:
            return {"state": "running"}
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "output": base64.b64encode(b'{"output": {}}').decode(),
        }


SLOW = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "slow.job",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        "cardea": {
            "version": 1,
            "effect": "effect",
            "executor": {"kind": "confined", "profile": "coding", "worker": "code-session"},
            "timeout_seconds": 5400,
        },
    }
)


def renewals(kernel: Kernel, task_id: str) -> int:
    with kernel.database.read() as conn:
        return int(
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM events WHERE event_type='task.lease_renewed' "
                "AND correlation_id=?",
                (task_id,),
            ).scalar_one()
        )


def test_a_long_call_keeps_a_short_lease_alive_with_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = kernel_for(tmp_path, SlowHost(2.0), monkeypatch, Registry((SLOW,), {}), "slow.job")
    task_id = submit(kernel, "slow.job", {})
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    # The 1 s lease outlived itself only because the heartbeat renewed it.
    assert renewals(kernel, task_id) >= 5
    assert receipt(kernel, task_id).state is OperationState.SUCCEEDED


def test_a_long_call_whose_lease_is_lost_is_stopped_and_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = SlowHost(30.0)
    kernel = kernel_for(tmp_path, host, monkeypatch, Registry((SLOW,), {}), "slow.job")
    # Renewal fails, as it does once recovery has replaced or expired the lease.
    monkeypatch.setattr(OperationRuntime, "renew", lambda self, *args: False)
    task_id = submit(kernel, "slow.job", {})
    started = time.monotonic()
    OperationRuntime(kernel).execute(task_id)
    assert time.monotonic() - started < 10
    assert host.stopped  # the worker's process tree was stopped
    assert receipt(kernel, task_id).state is OperationState.OUTCOME_UNKNOWN
    assert task(kernel, task_id).state is TaskState.FAILED


def test_a_lease_can_be_renewed_only_by_its_live_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardea.kernel.store import StoreError

    kernel = kernel_for(tmp_path, SlowHost(0.0), monkeypatch, Registry((SLOW,), {}), "slow.job")
    task_id = submit(kernel, "slow.job", {})
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    # The task finished, so it holds no lease any more: nobody can renew one.
    with pytest.raises(StoreError), kernel.database.engine.begin() as conn, borrow(conn) as tx:
        current = tx.get_task(task_id)
        tx.renew_lease(
            task_id,
            expected_revision=current.revision,
            lease_id="lease-gone",
            expires_at=now() + timedelta(hours=1),
            at=now(),
        )


# Dependencies for the gates (ADR-037 question 8): registries through the proxy, or the cache

PROXY = {"HTTPS_PROXY": "http://127.0.10.5:3128", "NO_PROXY": "127.0.11.1,localhost"}
# What the helper sets for a networked unit: the registries its profile's proxy allows.
WITH_REGISTRIES = {"CARDEA_REGISTRIES": "pypi.org,files.pythonhosted.org"}
WITHOUT_REGISTRIES = {"CARDEA_REGISTRIES": ""}


def gate_env_of(kernel: Kernel, task_id: str) -> dict[str, str]:
    output = receipt(kernel, task_id).result["output"]
    return cast(dict[str, str], json.loads(output["gates"][0]["output_tail"]))


def set_dependencies(tmp_path: Path, dependencies: str | None) -> None:
    path = tmp_path / "coding.json"
    config = json.loads(path.read_text())
    if dependencies is not None:
        config["dependencies"] = dependencies
    path.write_text(json.dumps(config))


@pytest.mark.parametrize(
    ("dependencies", "profile"),
    [("registries", WITH_REGISTRIES), ("cache", WITHOUT_REGISTRIES), (None, WITHOUT_REGISTRIES)],
)
def test_the_gates_get_the_proxy_only_when_the_profile_and_setting_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    dependencies: str | None,
    profile: dict[str, str],
) -> None:
    kernel, host = coding_kernel(tmp_path, monkeypatch, source)
    set_dependencies(tmp_path, dependencies)
    host.env |= PROXY | profile
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key=str(dependencies))
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    env = gate_env_of(kernel, task_id)
    if dependencies == "registries":
        assert {k: env[k] for k in PROXY} == PROXY
    else:
        # With a profile that allows no registry, the gates get no proxy variables either.
        assert not {k for k in env if k.lower().endswith("_proxy")}
    # Either way the gates get no credential: not the model's, not a planted host one.
    assert "MODEL_API_KEY" not in env and not set(PLANTED) & set(env)


@pytest.mark.parametrize(
    ("dependencies", "profile"),
    [
        # `cache` claims no network, but the profile's proxy allows registries to every
        # process in the unit, the harness and code in the checkout included.
        ("cache", WITH_REGISTRIES),
        (None, WITH_REGISTRIES),
        # `registries` on a profile whose proxy allows none: the gates could not install.
        ("registries", WITHOUT_REGISTRIES),
    ],
)
def test_a_setting_that_disagrees_with_the_profile_is_refused_before_anything_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    dependencies: str | None,
    profile: dict[str, str],
) -> None:
    kernel, host = coding_kernel(tmp_path, monkeypatch, source)
    set_dependencies(tmp_path, dependencies)
    host.env |= PROXY | profile
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key=f"x{dependencies}")
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    settled = receipt(kernel, task_id)
    assert settled.failure is not None and settled.failure.code == "dependencies_mismatch"
    # Refused before the harness ran: there is no change, no gate result and no object.
    assert settled.result is None or not settled.result.get("output")


def test_dependency_settings_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    from cardea_coding_ops.session import PROXY_NAMES, dependency_env, fixed_env

    from cardea.service.operations import OperationRejected

    # The host (a confined worker) may set any of these; the test sets what it needs.
    for name in (*PROXY_NAMES, "CARDEA_REGISTRIES"):
        monkeypatch.delenv(name, raising=False)
    # Not told by the helper (no networked unit): the cache is the only option.
    assert dependency_env({}) == {} and dependency_env({"dependencies": "cache"}) == {}
    with pytest.raises(OperationRejected):
        dependency_env({"dependencies": "internet"})
    with pytest.raises(OperationRejected) as untold:
        dependency_env({"dependencies": "registries"})
    assert untold.value.code == "dependencies_mismatch"
    monkeypatch.setenv("CARDEA_REGISTRIES", "pypi.org")
    with pytest.raises(OperationRejected) as cached:
        dependency_env({"dependencies": "cache"})
    assert cached.value.code == "dependencies_mismatch" and "pypi.org" in str(cached.value)
    # Registries need the profile's egress proxy; without one the call is refused.
    with pytest.raises(OperationRejected):
        dependency_env({"dependencies": "registries"})
    for url in ("http://127.0.0.1:3128", "http://10.0.0.1:3128", "http://proxy.example:3128"):
        monkeypatch.setenv("HTTPS_PROXY", url)
        with pytest.raises(OperationRejected):
            dependency_env({"dependencies": "registries"})
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.10.5:3128")
    assert dependency_env({"dependencies": "registries"}) == {
        "HTTPS_PROXY": "http://127.0.10.5:3128"
    }
    # A host owner cannot route the gates around the profile with gate_env.
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        with pytest.raises(OperationRejected):
            fixed_env({name: "http://127.0.10.9:3128"})


# Gates run agent-written code: they must not forge results or change the change

LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="needs /proc, PR_SET_DUMPABLE and PR_SET_CHILD_SUBREAPER (Linux)",
)

# Tries to write a forged result into the worker's standard output through /proc.
FORGE = """
import json, os
forged = json.dumps({"output": {"gates_passed": True, "forged": True}})
try:
    with open(f"/proc/{os.getppid()}/fd/1", "w") as stream:
        stream.write(forged)
    print("forge: written")
except OSError as error:
    print("forge: denied", type(error).__name__)
"""

# Leaves a detached grandchild that, unless it is killed, edits the change after its gate.
DETACH = """
import os, sys, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        time.sleep(1.5)
        with open("a.txt", "a") as stream:
            stream.write("tampered by a stray process\\n")
        time.sleep(5)
        os._exit(0)
    os._exit(0)
os.wait()
print("detached a child")
"""

PAUSE = "import time; time.sleep(3); print('second gate')"
KILL_WORKER = "import os, signal; os.kill(os.getppid(), signal.SIGKILL)"
EDIT = "open('a.txt', 'w').write('rewritten by a gate\\n'); print('edited')"


def with_gates(tmp_path: Path, *scripts: str) -> None:
    """Replace the fixture repository's gates with these Python scripts, in order."""
    folder = tmp_path / "scripts"
    gates = []
    for n, script in enumerate(scripts):
        path = folder / f"hostile-{n}.py"
        path.write_text(script)
        gates.append({"name": f"g{n}", "argv": [sys.executable, str(path)], "timeout_seconds": 30})
    registry = tmp_path / "repositories.json"
    document = json.loads(registry.read_text())
    document["repositories"]["app"]["gates"] = gates
    registry.write_text(json.dumps(document))


def stored_objects(kernel: Kernel, task_id: str) -> int:
    """Objects the call stored: its artifacts and output are named after its receipt."""
    prefix = receipt(kernel, task_id).receipt_id
    with kernel.database.read() as conn:
        return int(
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM objects WHERE resource_id LIKE ?", (f"{prefix}.%",)
            ).scalar_one()
        )


@LINUX
def test_a_gate_cannot_write_the_workers_result_through_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    with_gates(tmp_path, FORGE)
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key="forge")
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    output = receipt(kernel, task_id).result["output"]
    assert "forge: denied PermissionError" in output["gates"][0]["output_tail"]
    assert "forged" not in json.dumps(output)


@LINUX
def test_a_detached_gate_child_is_killed_before_it_can_change_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    with_gates(tmp_path, DETACH, PAUSE)
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key="detach")
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    output = receipt(kernel, task_id).result["output"]
    assert output["gates"][0]["stray_processes_killed"] >= 1
    assert [g["passed"] for g in output["gates"]] == [True, True]
    content = artifacts.read(kernel.database, output["artifacts"][0])
    (tmp_path / "d.bundle").write_bytes(content)
    check = tmp_path / "check"
    git(tmp_path, "clone", "--quiet", str(source), str(check))
    git(check, "fetch", "--quiet", str(tmp_path / "d.bundle"), "HEAD:refs/heads/d")
    assert git(check, "show", "d:a.txt") == "a\n"


def test_a_gate_that_kills_the_worker_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    with_gates(tmp_path, KILL_WORKER)
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key="kill")
    assert OperationRuntime(kernel).execute(task_id) != "succeeded"
    settled = receipt(kernel, task_id)
    # No result at all, so nothing forged: no output, no gate record, no stored change.
    assert settled.state is not OperationState.SUCCEEDED
    assert not (settled.result or {}).get("output")
    assert stored_objects(kernel, task_id) == 0


@pytest.mark.parametrize(
    "script",
    [
        EDIT,
        # Committing moves HEAD: the change is no longer the one bundled before the gates.
        "import subprocess; open('b.txt', 'w').write('b'); "
        "subprocess.run(['git', 'add', 'b.txt']); "
        "subprocess.run(['git', '-c', 'user.name=g', '-c', 'user.email=g@example.invalid', "
        "'commit', '-qm', 'gate commit'])",
    ],
)
def test_a_gate_that_changes_the_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path, script: str
) -> None:
    kernel, _ = coding_kernel(tmp_path, monkeypatch, source)
    with_gates(tmp_path, script)
    task_id = submit(kernel, "code.session", change({"a.txt": "a\n"}), key=str(hash(script)))
    assert OperationRuntime(kernel).execute(task_id) == "failed"
    settled = receipt(kernel, task_id)
    assert settled.failure is not None and settled.failure.code == "gate_changed_checkout"
    assert stored_objects(kernel, task_id) == 0


# The harness may commit itself; `changed` follows HEAD, not the working tree

COMMITTING_HARNESS = """
import subprocess, sys
ident = ["-c", "user.name=h", "-c", "user.email=h@example.invalid"]

def git(*args):
    subprocess.run(["git", *ident, *args], check=True, capture_output=True)

def commit(name):
    open(name, "w").write(name)
    git("add", name)
    git("commit", "-qm", name)

mode = sys.argv[1]
if mode != "none":
    git("checkout", "-q", "-b", "side")
    commit("side.txt")
    git("checkout", "-q", "main")
    commit("main.txt")
    if mode == "rebase":
        git("checkout", "-q", "side")
        git("rebase", "-q", "main")
    else:
        git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    # No remote-tracking ref survives, and the commits leave nothing in the tree.
    git("update-ref", "-d", "refs/remotes/origin/main")
"""


def run_session(tmp_path: Path, source: Path, mode: str) -> tuple[dict[str, Any], bytes]:
    from cardea_coding_ops.session import session

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = tmp_path / "committing.py"
    script.write_text(COMMITTING_HARNESS)
    entry = {"source": str(source), "gates": []}
    config: dict[str, Any] = {
        "harness": {"argv": [sys.executable, str(script), mode]},
        "repositories": {"app": entry},
    }
    arguments: dict[str, Any] = {"repository": "app", "base_ref": "main", "task": "go"}
    result = session(arguments, config, scratch)
    if isinstance(result, WithArtifacts):
        return result.output, result.artifacts["change.bundle"]
    assert isinstance(result, dict)
    return result, b""


@pytest.mark.parametrize("mode", ["rebase", "merge"])
def test_commits_made_by_the_harness_are_bundled_without_identity_or_origin_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path, mode: str
) -> None:
    monkeypatch.delenv("CARDEA_REPOSITORIES", raising=False)
    output, bundle = run_session(tmp_path, source, mode)
    assert output["changed"] is True
    head = output["head_sha"]
    assert isinstance(head, str) and head != output["base_sha"]
    assert bundle
    (tmp_path / "change.bundle").write_bytes(bundle)
    check = tmp_path / "check"
    git(tmp_path, "clone", "--quiet", str(source), str(check))
    git(check, "bundle", "verify", str(tmp_path / "change.bundle"))
    git(check, "fetch", "--quiet", str(tmp_path / "change.bundle"), "HEAD:refs/heads/change")
    assert git(check, "rev-parse", "change").strip() == head
    assert git(check, "show", "change:side.txt") == "side.txt"
    assert git(check, "show", "change:main.txt") == "main.txt"


def test_an_unchanged_checkout_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: Path
) -> None:
    monkeypatch.delenv("CARDEA_REPOSITORIES", raising=False)
    output, bundle = run_session(tmp_path, source, "none")
    assert output["changed"] is False and output["head_sha"] is None and not bundle
