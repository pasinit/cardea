"""Native A2A transport plus deterministic authority, budget and fault acceptance."""

import asyncio
import base64
import json
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import rfc8785
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError
from fastapi.testclient import TestClient

from cardea.runtime.a2a import CARD_PATH, discover, message
from cardea.service import host_operations
from cardea.service import kernel as handlers
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.delegation import child_request
from cardea.service.delegation_runtime import DelegationRuntime
from cardea.service.host_runtime import HostRuntime
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    DelegationTaskRequest,
    HostBudget,
    Permission,
    Principal,
    Provisioning,
    Signal,
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
        return self.validate("runtime")

    def runtime_token(self) -> str:
        return "runtime"

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in {"runtime", "supervisor", "outsider"}:
            raise Fault("invalid_credential", "Unknown fixture credential", 401)
        return Proof("spiffe", "test", token, time.time() + 3600)


class Host:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.starts = 0
        self.corrupt = False
        self.observer: Any = lambda: None

    def probe(self) -> None:
        pass

    def cleanup(self, job: str) -> dict[str, Any]:
        return {"cleaned": True}

    def stop(self, job: str) -> dict[str, Any]:
        return {"stopped": True}

    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None:
        self.starts += 1
        self.jobs[job] = host_operations.expected(operation, content)

    def status(self, job: str) -> dict[str, Any]:
        self.observer()
        output = {} if self.corrupt else self.jobs[job]
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "wall_ms": 1,
            "output": base64.b64encode(rfc8785.dumps(output)).decode(),
        }


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
    ) + tuple(
        Principal(principal_id=p, kind="agent", runtime_id="runtime")
        for p in ("coordinator", "worker")
    )
    rights = (*RIGHTS, "tasks.delegate", "host.sha256", "host.copy")
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
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=permissions,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions),
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
    yield instance
    instance.close()
    db.close()


def request(**changes: Any) -> DelegationTaskRequest:
    return DelegationTaskRequest.model_validate(
        {
            "request_id": "plan",
            "agent_id": "coordinator",
            "namespace": "test",
            "objective": "Verify at least one independent result",
            "grants": [*RIGHTS, "tasks.delegate", "host.sha256", "host.copy"],
            "children": [
                {"agent_id": "worker", "operation": "host.sha256", "payload": {"x": 1}},
                {"agent_id": "worker", "operation": "host.copy", "payload": {"y": 2}},
            ],
            **changes,
        }
    )


def submit(kernel: Kernel, **changes: Any) -> str:
    return kernel.submit(kernel.auth.validate("supervisor"), request(**changes)).task.task_id


def children(kernel: Kernel, parent: str) -> list[str]:
    with kernel.database.engine.begin() as conn:
        return [
            str(r[0])
            for r in conn.exec_driver_sql(
                "SELECT child_id FROM delegation_links WHERE parent_id=? ORDER BY slot", (parent,)
            )
        ]


def tick(kernel: Kernel, parent: str) -> str:
    return asyncio.run(
        DelegationRuntime(kernel).tick(parent, httpx.ASGITransport(app=create_app(kernel)))
    )


def test_native_delegation_and_single_execution_owner(kernel: Kernel) -> None:
    parent = submit(kernel)
    assert tick(kernel, parent) == "waiting"
    ids = children(kernel, parent)
    assert len(ids) == 2
    for child in ids:
        assert HostRuntime(kernel).execute(child) == "succeeded"
    assert tick(kernel, parent) == "succeeded"
    assert tick(kernel, parent) == "succeeded"
    assert cast(Host, kernel.host).starts == 2
    with TestClient(create_app(kernel)) as client:
        headers = {"Authorization": "Bearer supervisor"}
        output = client.get(f"/v0.2/tasks/{parent}/output", headers=headers).json()
        assert output["content"]["verified_children"] == 2
        assert output["content"]["usage"]["tool_milliseconds"] == 2


def test_native_duplicate_and_changed_message(kernel: Kernel) -> None:
    async def exercise() -> None:
        parent = submit(kernel)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(kernel)),
            headers={"Authorization": "Bearer runtime", "X-Cardea-Parent": parent},
        ) as client:
            peer = await discover(client)
            first = await peer.send_message(message(parent, request(), 0))
            again = await peer.send_message(message(parent, request(), 0))
            assert first.task.id == again.task.id
            altered = message(parent, request(), 0)
            data = json.loads(altered.message.parts[0].text)
            data["payload"] = {"changed": True}
            altered.message.parts[0].text = json.dumps(data)
            with pytest.raises(InvalidParamsError):
                await peer.send_message(altered)
            assert len(children(kernel, parent)) == 1

    asyncio.run(exercise())


def test_parent_budget_at_child_admission(kernel: Kernel) -> None:
    parent = submit(kernel, tool_milliseconds=10000)
    assert tick(kernel, parent) == "failed"
    assert len(children(kernel, parent)) == 1
    with pytest.raises(Fault):
        kernel.submit(
            kernel.auth.runtime_proof(),
            child_request(parent, request(tool_milliseconds=10000), 1),
            delegation=(parent, 1),
        )


@pytest.mark.parametrize("column", ["principal", "ancestor", "policy", "cancel"])
def test_live_ancestor_restriction_before_child_dispatch(kernel: Kernel, column: str) -> None:
    parent = submit(kernel)
    assert tick(kernel, parent) == "waiting"
    with kernel.database.engine.begin() as conn:
        if column == "principal":
            conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='supervisor'")
        elif column == "ancestor":
            conn.exec_driver_sql(
                "UPDATE principals SET disabled=1 WHERE principal_id='coordinator'"
            )
        elif column == "policy":
            conn.exec_driver_sql(
                "DELETE FROM policies WHERE principal_id='supervisor' AND operation='host.sha256'"
            )
    if column == "cancel":
        kernel.control(
            kernel.auth.validate("supervisor"), parent, Signal(request_id="stop"), "cancel"
        )
    for child in children(kernel, parent):
        state = HostRuntime(kernel).execute(child)
        if column == "policy" and child == children(kernel, parent)[1]:
            assert state == "succeeded"
        else:
            assert state in {"failed", "cancelled"}
    assert cast(Host, kernel.host).starts == (1 if column == "policy" else 0)


def test_child_failure_can_satisfy_explicit_partial_objective(kernel: Kernel) -> None:
    parent = submit(kernel)
    tick(kernel, parent)
    first, second = children(kernel, parent)
    host = cast(Host, kernel.host)
    host.corrupt = True
    assert HostRuntime(kernel).execute(first) == "failed"
    host.corrupt = False
    assert HostRuntime(kernel).execute(second) == "succeeded"
    assert tick(kernel, parent) == "succeeded"


def test_parent_cancel_during_worker_stops_publication(kernel: Kernel) -> None:
    parent = submit(kernel)
    tick(kernel, parent)
    host = cast(Host, kernel.host)
    host.observer = lambda: kernel.control(
        kernel.auth.validate("supervisor"), parent, Signal(request_id="stop"), "cancel"
    )
    assert HostRuntime(kernel).execute(children(kernel, parent)[0]) == "cancelled"
    assert tick(kernel, parent) == "cancelled"


def test_card_and_task_disclosure_require_bound_runtime(kernel: Kernel) -> None:
    parent = submit(kernel)
    with TestClient(create_app(kernel)) as client:
        for token in ("outsider", "supervisor"):
            assert (
                client.get(
                    CARD_PATH,
                    headers={"Authorization": f"Bearer {token}", "X-Cardea-Parent": parent},
                ).status_code
                == 403
            )
        assert client.get(CARD_PATH).status_code == 401


def test_no_alternate_child_admission(kernel: Kernel) -> None:
    parent = submit(kernel)
    with pytest.raises(Fault, match="bound peer"):
        kernel.submit(kernel.auth.validate("supervisor"), child_request(parent, request(), 0))


def test_native_cancel_list_history_and_cross_parent(kernel: Kernel) -> None:
    from a2a.utils.errors import A2AError

    async def exercise() -> None:
        parent = submit(kernel)
        other = submit(kernel, request_id="other")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(kernel)),
            headers={"Authorization": "Bearer runtime", "X-Cardea-Parent": parent},
        ) as client:
            peer = await discover(client)
            first = (await peer.send_message(message(parent, request(), 0))).task
            second = (await peer.send_message(message(parent, request(), 1))).task
            page = await peer.list_tasks(a2a.ListTasksRequest(page_size=1, history_length=0))
            assert len(page.tasks) == 1 and not page.tasks[0].history and page.next_page_token
            rest = await peer.list_tasks(
                a2a.ListTasksRequest(page_size=1, page_token=page.next_page_token)
            )
            assert {page.tasks[0].id, rest.tasks[0].id} == {first.id, second.id}
            cancelled = await peer.cancel_task(a2a.CancelTaskRequest(id=first.id))
            assert cancelled.status.state == a2a.TaskState.TASK_STATE_CANCELED
            assert (await peer.cancel_task(a2a.CancelTaskRequest(id=first.id))).id == first.id
            client.headers["X-Cardea-Parent"] = other
            with pytest.raises(A2AError):
                await peer.get_task(a2a.GetTaskRequest(id=first.id))

    asyncio.run(exercise())


def test_admission_race_cannot_mint_parent_budget(kernel: Kernel) -> None:
    from concurrent.futures import ThreadPoolExecutor

    parent = submit(kernel, tool_milliseconds=10000)

    def admit(slot: int) -> bool:
        try:
            kernel.submit(
                kernel.auth.runtime_proof(),
                child_request(parent, request(tool_milliseconds=10000), slot),
                delegation=(parent, slot),
            )
            return True
        except Fault as error:
            assert error.code == "parent_budget_exhausted"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(admit, (0, 1))) == 1
    assert len(children(kernel, parent)) == 1


@pytest.mark.parametrize(
    "stage",
    ["delegation.child_admitted", "delegation.after_peer", "delegation.publish_before_commit"],
)
def test_crash_boundaries_reconcile_committed_children(kernel: Kernel, stage: str) -> None:
    class Crash(BaseException):
        pass

    parent = submit(kernel)

    def crash(at: str) -> None:
        if at == stage:
            raise Crash()

    if stage == "delegation.publish_before_commit":
        tick(kernel, parent)
        for child in children(kernel, parent):
            HostRuntime(kernel).execute(child)
    kernel.fault = crash
    with pytest.raises(Crash):
        tick(kernel, parent)
    kernel.fault = lambda at: None
    tick(kernel, parent)
    for child in children(kernel, parent):
        HostRuntime(kernel).execute(child)
    assert tick(kernel, parent) == "succeeded"
    assert len(children(kernel, parent)) == 2
    assert cast(Host, kernel.host).starts == 2


def test_revoked_delegation_stops_child_without_waiting_for_coordinator(kernel: Kernel) -> None:
    parent = submit(kernel)
    tick(kernel, parent)
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM policies WHERE operation='tasks.delegate'")
    for child in children(kernel, parent):
        assert HostRuntime(kernel).execute(child) == "failed"
    assert cast(Host, kernel.host).starts == 0


def test_native_completed_cannot_override_invalid_artifact(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardea.service import delegation_runtime

    parent = submit(kernel)
    tick(kernel, parent)
    for child in children(kernel, parent):
        HostRuntime(kernel).execute(child)
    monkeypatch.setattr(delegation_runtime, "artifact_content", lambda task: {"forged": True})
    assert tick(kernel, parent) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_task(parent).failure is not None
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM objects WHERE resource_id=?", (f"{parent}.output",)
            ).scalar_one()
            == 0
        )


def test_peer_message_cannot_expand_grants(kernel: Kernel) -> None:
    parent = submit(kernel)
    child = child_request(parent, request(), 0)
    expanded = child.model_copy(update={"grants": (*child.grants, "host.copy")})
    with pytest.raises(Fault, match="admitted objective"):
        kernel.submit(kernel.auth.runtime_proof(), expanded, delegation=(parent, 0))
    assert children(kernel, parent) == []


def test_child_cannot_bypass_allocation_with_legacy_grandchild(kernel: Kernel) -> None:
    from cardea.service.models import TaskRequest

    parent = submit(kernel)
    tick(kernel, parent)
    child = children(kernel, parent)[0]
    with pytest.raises(Fault, match="installed peer admission"):
        kernel.submit(
            kernel.auth.validate("supervisor"),
            TaskRequest(
                request_id="grandchild",
                agent_id="worker",
                namespace="test",
                objective="Attempt an unallocated write",
                payload={"escape": True},
                parent_task_id=child,
            ),
        )
    with kernel.database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM task_bindings WHERE parent_task_id=?", (child,)
            ).scalar_one()
            == 0
        )


def test_failed_parent_remains_stopping_across_reconstruction(kernel: Kernel) -> None:
    from datetime import timedelta

    from cardea.kernel.models import DispatchLease, TaskState

    parent = submit(kernel)
    tick(kernel, parent)
    first, second = children(kernel, parent)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        child = tx.get_task(first)
        at = now()
        tx.change_task(
            first,
            TaskState.RUNNING,
            expected_revision=child.revision,
            at=at,
            lease=DispatchLease(
                lease_id="test-lease",
                worker_id="runtime",
                acquired_at=at,
                expires_at=at + timedelta(seconds=30),
            ),
        )
    assert DelegationRuntime(kernel).stopping(parent, "peer_rejected") == "waiting"
    with pytest.raises(Fault) as denied:
        kernel.submit(
            kernel.auth.runtime_proof(), child_request(parent, request(), 0), delegation=(parent, 0)
        )
    assert denied.value.code == "cancel_requested"
    assert tick(kernel, parent) == "waiting"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        child = tx.get_task(first)
        tx.change_task(
            first,
            TaskState.CANCELLED,
            expected_revision=child.revision,
            lease_id="test-lease",
            at=now(),
        )
    assert tick(kernel, parent) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        failure = tx.get_task(parent).failure
        assert failure and failure.code == "peer_rejected"
        assert tx.get_task(second).state is TaskState.CANCELLED
    assert cast(Host, kernel.host).starts == 0


def test_m3_upgrade_preserves_existing_host_receipt(kernel: Kernel) -> None:
    from cardea.service.models import HostTaskRequest

    task = kernel.submit(
        kernel.auth.validate("supervisor"),
        HostTaskRequest(
            request_id="old-host",
            agent_id="worker",
            namespace="test",
            objective="Existing job",
            payload={"old": True},
            operation="host.sha256",
            grants=(*RIGHTS, "host.sha256"),
        ),
    ).task.task_id
    assert HostRuntime(kernel).execute(task) == "succeeded"
    with kernel.database.engine.begin() as conn:
        before = conn.exec_driver_sql(
            "SELECT snapshot FROM operations WHERE task_id=?", (task,)
        ).scalar_one()
        conn.exec_driver_sql("DROP TABLE recovery_epochs")
        conn.exec_driver_sql("DROP TABLE trigger_events")
        conn.exec_driver_sql("DROP TABLE triggers")
        conn.exec_driver_sql("DROP TABLE effect_scopes")
        conn.exec_driver_sql("DROP TABLE delegation_links")
        conn.exec_driver_sql("UPDATE cardea_alembic_version SET version_num='m3_001'")
    kernel.database.migrate()
    kernel.database.check_version()
    with kernel.database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT snapshot FROM operations WHERE task_id=?", (task,)
            ).scalar_one()
            == before
        )
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM delegation_links").scalar_one() == 0


def test_parent_deadline_restricts_queued_children(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardea.service import delegation

    parent = submit(kernel)
    tick(kernel, parent)
    later = now() + timedelta(hours=2)
    monkeypatch.setattr(delegation, "now", lambda: later)
    for child in children(kernel, parent):
        assert HostRuntime(kernel).execute(child) == "failed"
    assert cast(Host, kernel.host).starts == 0


@pytest.mark.parametrize("boundary", ["authentication", "admission"])
def test_blocked_peer_call_does_not_block_other_requests(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from threading import Event

    from cardea.runtime import a2a as peer_module

    parent = submit(kernel)
    entered, release, expired = Event(), Event(), Event()
    target = peer_module if boundary == "authentication" else kernel
    attribute = "credential" if boundary == "authentication" else "submit"
    original = getattr(target, attribute)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        if not release.wait(3):
            expired.set()
        return original(*args, **kwargs)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(kernel)),
            base_url="http://cardea",
            headers={"Authorization": "Bearer runtime", "X-Cardea-Parent": parent},
        ) as client:
            peer = await discover(client)
            monkeypatch.setattr(target, attribute, blocked)
            pending = asyncio.create_task(peer.send_message(message(parent, request(), 0)))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                response = await client.get(
                    "/v0.2/system", headers={"Authorization": "Bearer supervisor"}
                )
                assert response.status_code == 200
                assert not expired.is_set(), "Other requests waited on the blocked peer call"
            finally:
                release.set()
                await pending

    asyncio.run(exercise())


def test_cancel_wins_over_concurrent_peer_rejection(kernel: Kernel) -> None:
    parent = submit(kernel)

    def interrupt(stage: str) -> None:
        if stage == "delegation.after_peer":
            kernel.control(
                kernel.auth.validate("supervisor"), parent, Signal(request_id="stop"), "cancel"
            )
            raise InvalidParamsError("Admission rejected during cancellation")

    kernel.fault = interrupt
    assert tick(kernel, parent) == "cancelled"
    linked = children(kernel, parent)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_task(parent).state.value == "cancelled"
        assert all(tx.get_task(child).state.value == "cancelled" for child in linked)


def test_inspection_reads_committed_policy_without_waiting_for_writer(kernel: Kernel) -> None:
    parent = submit(kernel)
    proof = kernel.auth.validate("supervisor")
    with kernel.database.engine.begin() as writer:
        writer.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='supervisor'")
        assert kernel.inspect(proof, parent).task.task_id == parent
    # A new request sees the committed revocation; no authority is cached.
    with pytest.raises(Fault):
        kernel.inspect(proof, parent)


def test_read_snapshot_cannot_mutate_and_does_not_weaken_writer(kernel: Kernel) -> None:
    import sqlalchemy as sa

    with (
        pytest.raises(sa.exc.OperationalError, match="readonly"),
        kernel.database.read() as reader,
    ):
        reader.exec_driver_sql("UPDATE principals SET disabled=1")
    with kernel.database.engine.begin() as writer:
        writer.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='outsider'")
    with kernel.database.read() as reader:
        assert (
            reader.exec_driver_sql(
                "SELECT disabled FROM principals WHERE principal_id='outsider'"
            ).scalar_one()
            == 1
        )
