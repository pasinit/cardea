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
from cardea.service.operations import OperationDescriptor, Registry

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
) -> Kernel:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.02)
    # Short heartbeat periods, so a lost lease shows within a test's time.
    monkeypatch.setattr(operation_runtime, "HEARTBEAT_LEASE_SECONDS", 1.0)
    monkeypatch.setattr(operation_runtime, "HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    db = Database(tmp_path / "data")
    db.migrate()
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*RIGHTS, operation, "repo:app")
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
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    return Kernel(db, cast(Authentication, Auth()), operations=registry, workers=Workers(host))


def submit(
    kernel: Kernel,
    operation: str,
    arguments: dict[str, Any],
    key: str = "one",
    grants: tuple[str, ...] | None = None,
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


def coding_config(tmp_path: Path, source: Path, gate_exit: int = 0, **harness: Any) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "harness.py").write_text(HARNESS)
    (scripts / "gate.py").write_text(GATE)
    config = {
        "repositories": {
            "app": {
                "source": str(source),
                "gates": [
                    {"name": "lint", "argv": [sys.executable, str(scripts / "gate.py"), "0"]},
                    {
                        "name": "test",
                        "argv": [sys.executable, str(scripts / "gate.py"), str(gate_exit)],
                    },
                ],
            }
        },
        "harness": {
            "argv": [sys.executable, str(scripts / "harness.py")],
            "passthrough": ["MODEL_API_KEY"],
            **harness,
        },
    }
    path = tmp_path / "coding.json"
    path.write_text(json.dumps(config))
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
        {"CARDEA_CODING_CONFIG": str(config), "MODEL_API_KEY": "model-key", **PLANTED},
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
    host.start_confined = lambda job, *args: started.append(job) or real(job, *args)  # type: ignore[method-assign]
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
    tmp_path: Path, source: Path
) -> None:
    from cardea_coding_ops.session import session

    from cardea.service.operations import OperationRejected

    config = json.loads(coding_config(tmp_path, source).read_text())
    config["harness"]["passthrough"] = ["GITHUB_TOKEN"]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(OperationRejected, match="GITHUB_TOKEN"):
        session(change({"a": "a"}), config, scratch)
    with pytest.raises(OperationRejected, match="not allowed"):
        session({**change({}), "repository": "other"}, config, scratch)


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
