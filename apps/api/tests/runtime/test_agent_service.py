"""Portable authenticated-boundary tests with injected model and host effects."""

import asyncio
import base64
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import Tool

from cardea.runtime.incident_tool import compile_incidents
from cardea.runtime.incidents import IncidentBatch
from cardea.runtime.openrouter import OpenRouterModel, Proposal, Usage
from cardea.service import agent_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_runtime import AgentRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentInput,
    AgentTaskRequest,
    HostBudget,
    ModelBudget,
    Permission,
    Principal,
    Provisioning,
    Signal,
    now,
)


class Client:
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
        return proof("runtime")


class Model:
    model = "fixture/model"
    calls = 0

    async def propose(
        self, objective: str, context: dict[str, Any], tools: list[Tool], maximum: int
    ) -> Proposal:
        self.calls += 1
        return Proposal(
            f"response-{self.calls}",
            self.model,
            f"call-{self.calls}",
            "compile_incidents",
            {"batch": context},
            Usage(10, 20, 30, 0.0001),
        )


class Host:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}
        self.starts = 0
        self.cleaned: set[str] = set()

    def probe(self) -> None:
        pass

    def start(
        self, job: str, content: bytes, deadline: float, operation: str = "host.sha256"
    ) -> None:
        assert operation == "tools.incidents"
        assert job not in self.content and job not in self.cleaned
        self.content[job] = content
        self.starts += 1

    def status(self, job: str) -> dict[str, Any]:
        result = compile_incidents(IncidentBatch.model_validate_json(self.content[job]))
        return {
            "state": "finished",
            "result": "success",
            "exit_status": "0",
            "wall_ms": 2,
            "output": base64.b64encode(result.model_dump_json().encode()).decode(),
        }

    def stop(self, job: str) -> dict[str, Any]:
        return {"stopped": True}

    def cleanup(self, job: str) -> dict[str, Any]:
        self.cleaned.add(job)
        return {"cleaned": True}


def proof(subject: str = "supervisor") -> Proof:
    return Proof("spiffe", "fixture", subject, time.time() + 60)


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", Client)
    monkeypatch.setattr(agent_runtime, "LEASE_SECONDS", 0.5)
    database = Database(tmp_path / "data")
    database.migrate()
    principals = tuple(
        Principal(principal_id=p, kind="service", auth_kind="spiffe", issuer="fixture", subject=p)
        for p in ("supervisor", "runtime", "outsider")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    permissions = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=r,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for r in (*RIGHTS, "models.invoke", "models.disclose", "tools.incidents", "inputs.append")
    )
    provision(
        database,
        Provisioning(
            principals=principals,
            permissions=permissions,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in permissions),
            host_budgets=(
                HostBudget(
                    principal_id="supervisor",
                    namespace="test",
                    tool_milliseconds=100000,
                    output_bytes=1000000,
                ),
            ),
            model_budgets=(
                ModelBudget(principal_id="supervisor", namespace="test", calls=10, tokens=1000000),
            ),
        ),
    )
    instance = Kernel(
        database, cast(Authentication, Auth()), host=Host(), model=cast(OpenRouterModel, Model())
    )
    try:
        yield instance
    finally:
        instance.close()
        database.close()


def batch(extra: bool = False) -> IncidentBatch:
    return IncidentBatch.model_validate(
        {
            "records": [{"id": "INC-1", "title": "Fictional outage", "severity": "high"}]
            + ([{"id": "INC-2", "title": "Fictional delay", "severity": "low"}] if extra else [])
        }
    )


def request() -> AgentTaskRequest:
    return AgentTaskRequest(
        request_id="one",
        agent_id="agent",
        namespace="test",
        objective="Index incidents",
        inputs=batch(),
        grants=(*RIGHTS, "models.invoke", "models.disclose", "tools.incidents", "inputs.append"),
    )


def submit(kernel: Kernel) -> str:
    return kernel.submit(proof(), request()).task.task_id


def execute(kernel: Kernel, task: str) -> str:
    return asyncio.run(AgentRuntime(kernel).round(task))


def update(kernel: Kernel, task: str) -> None:
    kernel.append_input(proof(), task, AgentInput(request_id="update", inputs=batch(True)))


@pytest.mark.parametrize("early", [False, True])
def test_two_rounds_wait_input_and_verified_versions(kernel: Kernel, early: bool) -> None:
    task = submit(kernel)
    if early:
        update(kernel, task)
    assert execute(kernel, task) == "waiting"
    assert cast(Model, kernel.model).calls == 1
    if not early:
        update(kernel, task)
    assert AgentRuntime(kernel).wake(task) == "queued"
    assert execute(kernel, task) == "succeeded"
    assert kernel.inspect(proof(), task).task.verification is not None
    assert cast(Model, kernel.model).calls == 2 and cast(Host, kernel.host).starts == 2
    assert len(cast(Host, kernel.host).cleaned) == 2
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task, "1.model").usage.model_tokens == 30
        assert conn.exec_driver_sql(
            "SELECT SUM(tokens),SUM(settled) FROM agent_effects WHERE task_id=?", (task,)
        ).one() == (60, 4)
    # Idempotent delivery survives terminal state and never publishes another version.
    update(kernel, task)
    assert execute(kernel, task) == "succeeded"
    assert cast(Model, kernel.model).calls == 2


class Crash(BaseException):
    pass


class LeaseClock:
    """The runtime's clock: real time plus an offset the test advances.

    A crashing round runs under a long lease, so a slow machine can never expire it
    mid-round; the test then moves past the lease instead of sleeping until it expires.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = timedelta()
        monkeypatch.setattr(agent_runtime, "LEASE_SECONDS", 60)
        monkeypatch.setattr(agent_runtime, "now", lambda: now() + self.offset)

    def expire_lease(self) -> None:
        self.offset += timedelta(seconds=61)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> LeaseClock:
    return LeaseClock(monkeypatch)


@pytest.mark.parametrize("stage", ["agent.model.settle_committed", "agent.publish_committed"])
def test_committed_progress_resumes_without_model_replay(
    kernel: Kernel, clock: LeaseClock, stage: str
) -> None:
    task = submit(kernel)

    def fault(current: str) -> None:
        if current == stage:
            raise Crash()

    kernel.fault = fault
    with pytest.raises(Crash):
        execute(kernel, task)
    kernel.fault = lambda _: None
    clock.expire_lease()
    assert execute(kernel, task) == "waiting"
    assert cast(Model, kernel.model).calls == 1
    assert cast(Host, kernel.host).starts == 1


@pytest.mark.parametrize(
    "stage", ["agent.model.intent_committed", "agent.tool.intent_committed", "agent.tool.started"]
)
def test_unknown_intent_never_replayed(kernel: Kernel, clock: LeaseClock, stage: str) -> None:
    task = submit(kernel)

    def fault(current: str) -> None:
        if current == stage:
            raise Crash()

    kernel.fault = fault
    with pytest.raises((Crash, BaseExceptionGroup)):
        execute(kernel, task)
    calls, starts = cast(Model, kernel.model).calls, cast(Host, kernel.host).starts
    kernel.fault = lambda _: None
    clock.expire_lease()
    assert execute(kernel, task) == "failed"
    assert cast(Model, kernel.model).calls == calls and cast(Host, kernel.host).starts == starts
    with kernel.database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT COUNT(*) FROM operations WHERE task_id=? AND state='outcome_unknown'",
                (task,),
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("right", ["models.invoke", "models.disclose", "tools.incidents"])
def test_revocation_before_dispatch_has_no_effect(kernel: Kernel, right: str) -> None:
    task = submit(kernel)
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM policies WHERE operation=? AND principal_id='supervisor'", (right,)
        )
    assert execute(kernel, task) == "failed"
    assert cast(Model, kernel.model).calls == 0 and cast(Host, kernel.host).starts == 0


def test_exhausted_model_admission_and_cross_supervisor(kernel: Kernel) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE model_budget_limits SET tokens=1")
    with pytest.raises(Fault, match="insufficient"):
        submit(kernel)
    with pytest.raises(Fault):
        kernel.submit(proof("outsider"), request())


def test_input_conflict_substitution_and_cancellation(kernel: Kernel) -> None:
    task = submit(kernel)
    assert execute(kernel, task) == "waiting"
    with pytest.raises(Fault):
        kernel.append_input(
            proof("outsider"), task, AgentInput(request_id="update", inputs=batch(True))
        )
    with pytest.raises(Fault, match="add one"):
        kernel.append_input(proof(), task, AgentInput(request_id="update", inputs=batch()))
    update(kernel, task)
    with pytest.raises(Fault, match="different bytes"):
        kernel.append_input(proof(), task, AgentInput(request_id="update", inputs=batch()))
    kernel.control(proof(), task, Signal(request_id="cancel"), "cancel")
    assert execute(kernel, task) == "cancelled"


def test_directory_rescan_and_denied_symlink(tmp_path: Path) -> None:
    from cardea.runtime.directory import snapshot

    root = tmp_path / "inputs"
    root.mkdir()
    first = root / "one.json"
    first.write_text(batch().records[0].model_dump_json())
    assert snapshot(root) == batch()
    (root / "two.json").write_text(batch(True).records[1].model_dump_json())
    assert snapshot(root) == batch(True)
    (root / "two.json").unlink()
    (root / "two.json").symlink_to(first)
    with pytest.raises(OSError):
        snapshot(root)


def test_model_allocation_serializes_competing_effects(kernel: Kernel) -> None:
    from concurrent.futures import ThreadPoolExecutor

    tasks = [
        kernel.submit(
            proof(), request().model_copy(update={"request_id": f"task-{i}"})
        ).task.task_id
        for i in range(4)
    ]
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE model_budget_limits SET calls=1")
    for task in tasks:
        AgentRuntime(kernel).prepare(task)

    def reserve(task: str) -> bool:
        try:
            AgentRuntime(kernel).intent(task, "model", {"input": {}})
            return True
        except Fault:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, tasks)) == 1


def test_provider_overage_recorded_without_clamping(kernel: Kernel) -> None:
    task = submit(kernel)
    runtime = AgentRuntime(kernel)
    runtime.prepare(task)
    runtime.intent(task, "model", {"input": {}})
    runtime.model_settle(
        task,
        Proposal(
            "overage",
            "fixture/model",
            "call",
            "compile_incidents",
            {"batch": batch().model_dump()},
            Usage(50000, 100, 50100, 0.1),
        ),
    )
    assert kernel.inspect(proof(), task).task.state.value == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert tx.get_operation(task, "0.model").usage.model_tokens == 50100
        assert (
            conn.exec_driver_sql(
                "SELECT tokens FROM agent_effects WHERE task_id=?", (task,)
            ).scalar_one()
            == 50100
        )
    assert cast(Host, kernel.host).starts == 0


def test_model_allocation_revoked_during_wait(kernel: Kernel) -> None:
    task = submit(kernel)
    assert execute(kernel, task) == "waiting"
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM model_budget_limits")
    update(kernel, task)
    assert AgentRuntime(kernel).wake(task) == "failed"
    assert cast(Model, kernel.model).calls == 1


def test_m3_migration_rollback_and_old_backup(tmp_path: Path) -> None:
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from cardea.service.admin import initialize, restore

    database = Database(tmp_path / "old")
    config = Config()
    config.set_main_option("script_location", str(Path(handlers.__file__).parent / "migrations"))
    with database.engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "m2_001")
    backup = tmp_path / "backup.sqlite3"
    database.backup(backup)

    def fail(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TABLE agent_effects" in statement:
            raise RuntimeError("migration interrupted")

    sa.event.listen(database.engine, "before_cursor_execute", fail)
    with pytest.raises(RuntimeError):
        database.migrate()
    sa.event.remove(database.engine, "before_cursor_execute", fail)
    with database.engine.begin() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM cardea_alembic_version").scalar_one()
            == "m2_001"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='model_budget_limits'"
            ).first()
            is None
        )
    database.close()
    restore(tmp_path / "restored", backup)
    initialize(tmp_path / "restored")
    restored = Database(tmp_path / "restored")
    restored.check_version()
    restored.close()


def test_agent_receipt_is_latest_operation(kernel: Kernel) -> None:
    from cardea.service.app import receipt_operation

    task = submit(kernel)
    with kernel.database.engine.begin() as conn, pytest.raises(Fault, match="not admitted"):
        receipt_operation(conn, "incident-agent-v1", task)
    update(kernel, task)
    assert execute(kernel, task) == "waiting"
    assert AgentRuntime(kernel).wake(task) == "queued"
    assert execute(kernel, task) == "succeeded"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        operation = receipt_operation(conn, "incident-agent-v1", task)
        assert operation == "1.tool"
        assert tx.get_operation(task, operation).state.value == "succeeded"
