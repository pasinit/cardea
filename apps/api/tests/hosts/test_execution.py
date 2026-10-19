"""Portable authority/recovery checks; these fakes do not establish Debian isolation."""

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from fastapi.testclient import TestClient

from cardea.kernel.models import OperationState, ResourceRef, TaskState
from cardea.service import host_runtime
from cardea.service import kernel as handlers
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    HostBudget,
    HostTaskRequest,
    Permission,
    Principal,
    Provisioning,
    Signal,
    TaskRequest,
    now,
)


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def send_in_transaction(self, *args: Any, **kwargs: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in {"supervisor", "outsider"}:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


class Host:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.content = b""
        self.observer: Callable[[], None] = lambda: None
        self.corrupt = False

    def probe(self) -> None:
        pass

    def cleanup(self, job: str) -> dict[str, Any]:
        return {"cleaned": True}

    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None:
        self.starts += 1
        self.content = content

    def status(self, job: str) -> dict[str, Any]:
        self.observer()
        value = {"sha256": hashlib.sha256(self.content).hexdigest(), "bytes": len(self.content)}
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "output": base64.b64encode(json.dumps({} if self.corrupt else value).encode()).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        self.stops += 1
        return {"stopped": True}


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    db = Database(tmp_path / "data")
    db.migrate()
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
    rights = (*RIGHTS, "host.sha256")
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for r in rights
    )
    platform = tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions)
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=permissions,
            platform=platform,
            host_budgets=(
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=1000000,
                    output_bytes=10000000,
                ),
            ),
        ),
    )
    instance = Kernel(db, cast(Authentication, Auth()), host=Host())
    try:
        yield instance
    finally:
        instance.close()
        db.close()


def request(**changes: Any) -> HostTaskRequest:
    return HostTaskRequest.model_validate(
        {
            "request_id": "one",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Hash declared input",
            "payload": {"value": 1},
            "operation": "host.sha256",
            "grants": [*RIGHTS, "host.sha256"],
            **changes,
        }
    )


def admit(kernel: Kernel, **changes: Any) -> str:
    return kernel.submit(kernel.auth.validate("supervisor"), request(**changes)).task.task_id


def state(kernel: Kernel, task_id: str) -> tuple[str, str]:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return tx.get_task(task_id).state.value, tx.get_operation(task_id, "execute").state.value


def test_success_duplicate_and_checked_artifact(kernel: Kernel) -> None:
    task_id = admit(kernel)
    assert admit(kernel) == task_id
    with pytest.raises(Fault, match="different arguments"):
        admit(kernel, payload={"value": 2})
    runtime = HostRuntime(kernel)
    assert runtime.execute(task_id) == "succeeded"
    assert runtime.execute(task_id) == "succeeded"
    host = cast(Host, kernel.host)
    assert host.starts == 1 and host.stops >= 1
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        assert task.verification and task.verification.passed
        output = tx.latest_object(ResourceRef(namespace="test", resource_id=f"{task_id}.output"))
        assert (
            json.loads(tx.read_object(output))["sha256"]
            == hashlib.sha256(rfc8785.dumps({"value": 1})).hexdigest()
        )
        receipt = tx.get_operation(task_id, "execute")
        assert receipt.usage.tool_milliseconds == 10000
        assert receipt.request.arguments["reserved_output_bytes"] == 65536


@pytest.mark.parametrize("phase", ["admission", "dispatch", "publication"])
@pytest.mark.parametrize("mode", ["allow", "delegate", "platform", "disabled"])
def test_current_ceiling_at_each_boundary(kernel: Kernel, phase: str, mode: str) -> None:
    def revoke() -> None:
        with kernel.database.engine.begin() as conn:
            if mode == "disabled":
                conn.exec_driver_sql(
                    "UPDATE principals SET disabled=1 WHERE principal_id='supervisor'"
                )
            else:
                conn.exec_driver_sql(
                    "DELETE FROM policies WHERE principal_id=? "
                    "AND operation='host.sha256' AND mode=?",
                    (
                        "platform" if mode == "platform" else "supervisor",
                        "allow" if mode == "platform" else mode,
                    ),
                )

    if phase == "admission":
        revoke()
        with pytest.raises(Fault):
            admit(kernel)
        assert cast(Host, kernel.host).starts == 0
        return
    task_id = admit(kernel)
    if phase == "dispatch":
        revoke()
    else:
        cast(Host, kernel.host).observer = revoke
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == (1 if phase == "publication" else 0)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_task(task_id).verification is None
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM objects").scalar_one() == 0


def test_cancel_stops_before_terminal_ack(kernel: Kernel) -> None:
    task_id = admit(kernel)
    host = cast(Host, kernel.host)

    def cancel() -> None:
        result = kernel.control(
            kernel.auth.validate("supervisor"), task_id, Signal(request_id="stop"), "cancel"
        )
        assert result.task.state is TaskState.RUNNING

    host.observer = cancel
    assert HostRuntime(kernel).execute(task_id) == "cancelled"
    assert host.stops >= 1
    assert state(kernel, task_id) == ("cancelled", "cancelled")


def test_successful_exit_cannot_bypass_verifier(kernel: Kernel) -> None:
    cast(Host, kernel.host).corrupt = True
    task_id = admit(kernel)
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert state(kernel, task_id) == ("failed", "failed")


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "point,starts",
    [
        ("host.intent_committed", 0),
        ("host.started", 1),
        ("host.before_settle", 1),
        ("host.settle_before_commit", 1),
    ],
)
def test_interrupted_intent_never_relaunches(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch, point: str, starts: int
) -> None:
    task_id = admit(kernel)

    def fault(stage: str) -> None:
        if stage == point:
            raise Crash()

    kernel.fault = fault
    with pytest.raises(Crash):
        HostRuntime(kernel).execute(task_id)
    assert cast(Host, kernel.host).starts == starts
    kernel.fault = lambda stage: None
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
        assert task.lease
        expired = task.lease.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(host_runtime, "now", lambda: expired)
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == starts
    assert state(kernel, task_id) == ("failed", OperationState.OUTCOME_UNKNOWN.value)


def test_api_requires_explicit_grant_and_rejects_arbitrary_commands(kernel: Kernel) -> None:
    with TestClient(create_app(kernel)) as client:
        headers = {"Authorization": "Bearer supervisor"}
        body = request().model_dump(mode="json")
        assert client.post("/v0.2/host-tasks", json=body).status_code == 401
        for extra in (
            {"command": "id"},
            {"operation": "shell"},
            {"grants": list(RIGHTS)},
            {"parent_task_id": "parent"},
        ):
            assert (
                client.post("/v0.2/host-tasks", json=body | extra, headers=headers).status_code
                == 422
            )
        response = client.post("/v0.2/host-tasks", json=body, headers=headers)
        assert response.status_code == 202
        task_id = response.json()["task"]["task_id"]
        assert (
            client.get(
                f"/v0.2/tasks/{task_id}", headers={"Authorization": "Bearer outsider"}
            ).status_code
            == 404
        )
        kernel.host = None
        assert client.post("/v0.2/host-tasks", json=body, headers=headers).status_code == 503
    assert (
        "host.sha256"
        not in TaskRequest(
            request_id="a", agent_id="a", namespace="a", objective="a", payload={}
        ).grants
    )


def test_revocation_between_intent_and_launch(kernel: Kernel) -> None:
    task_id = admit(kernel)

    def revoke(stage: str) -> None:
        if stage == "host.intent_committed":
            with kernel.database.engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM policies WHERE operation='host.sha256'")

    kernel.fault = revoke
    assert HostRuntime(kernel).execute(task_id) == "failed"
    assert cast(Host, kernel.host).starts == 0
    assert state(kernel, task_id) == ("failed", "failed")


def test_worker_deadline_attenuated_to_policy_expiry(kernel: Kernel) -> None:
    task_id = admit(kernel)
    expiry = time.time() + 10
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE policies SET expires_at=? WHERE operation='host.sha256'", (expiry,)
        )
    host = cast(Host, kernel.host)
    original = host.start

    def start(job: str, content: bytes, deadline: float, operation: str = "host.sha256") -> None:
        assert deadline <= expiry
        original(job, content, deadline)

    host.start = start  # type: ignore[method-assign]
    assert HostRuntime(kernel).execute(task_id) == "succeeded"


def test_cannot_disable_host_with_pending_recovery(kernel: Kernel) -> None:
    admit(kernel)
    with pytest.raises(Fault) as denied:
        Kernel(kernel.database, kernel.auth)
    assert denied.value.code == "host_required_for_recovery"
