"""Supervisor-managed schedules, run expectations and run evidence (ADR-029)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from pydantic import BaseModel, ValidationError
from test_agent_runs import (
    GRANTS,
    Auth,
    FakeClient,
    FakeSession,
    Tools,
    call,
    descriptor,
    routine,
    run_request,
    submit,
)

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentRunRequest,
    Permission,
    Principal,
    Provisioning,
    TriggerSpec,
)
from cardea.service.operations import Reconciliation, Registry
from cardea.service.responses import (
    AgentReceipts,
    AgentRunOutput,
    ChildrenView,
    HostBudgetView,
    OperationCatalog,
    ReconcileResult,
    RunOutput,
    RunView,
    SystemView,
    TaskReceipt,
    TriggerList,
    TriggerRemoved,
    TriggerScheduled,
)
from cardea.service.triggers import Scheduler

SCHEDULE_GRANTS = (*GRANTS, "tasks.schedule")
HEADERS = {"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def document(
    rights: tuple[str, ...] = SCHEDULE_GRANTS, triggers: tuple[TriggerSpec, ...] = ()
) -> Provisioning:
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
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        for right in rights
    )
    return Provisioning(
        principals=principals,
        permissions=granted,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        triggers=triggers,
    )


@pytest.fixture
def tools() -> Tools:
    return Tools()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr(handlers, "now", value)
    return value


@pytest.fixture
def kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: Tools,
    session: FakeSession,
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    registry = Registry(
        (
            descriptor("mail.send", "effect", idempotency_scope=["/to"]),
            descriptor("listings.search", "read"),
        ),
        {"test.mail.send": tools.send, "test.listings.search": tools.search},
    )
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(), registry)
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, session=session)
    try:
        yield instance
    finally:
        db.close()


def template(**changes: Any) -> dict[str, Any]:
    request = run_request(grants=list(SCHEDULE_GRANTS), **changes).model_dump(mode="json")
    del request["request_id"], request["idempotency_context"]
    return request


def routine_spec(**changes: Any) -> TriggerSpec:
    return TriggerSpec.model_validate(
        {
            "trigger_id": "loft-search",
            "supervisor_id": "supervisor",
            "schedule": "30 8 * * *",
            "timezone": "Europe/London",
            "misfire": "run_once_late",
            "template_kind": "agent-run",
            "template": template(),
            **changes,
        }
    )


def put(client: TestClient, spec: TriggerSpec, token: str = "supervisor") -> Any:
    return client.put(
        f"/v0.2/triggers/{spec.trigger_id}",
        json=spec.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {token}", "X-Cardea-Auth": "spiffe"},
    )


# Supervisor-managed schedules


def test_supervisor_schedules_an_agent_run_per_slot(kernel: Kernel, clock: Clock) -> None:
    client = TestClient(create_app(kernel))
    response = put(client, routine_spec())
    assert response.status_code == 200, response.text
    assert response.json()["change"] == "installed"
    assert put(client, routine_spec()).json()["change"] == "unchanged"
    clock.now = datetime(2026, 10, 1, 7, 30, 30, tzinfo=UTC)
    assert Scheduler(kernel, clock).tick() == {"loft-search": "admitted"}
    with kernel.database.read() as conn:
        task_id, request_json = conn.exec_driver_sql(
            "SELECT task_id,request_json FROM task_bindings"
        ).one()
    request = AgentRunRequest.model_validate_json(request_json)
    assert request.request_id == "loft-search.20261001T073000Z"
    assert request.idempotency_context == request.request_id
    assert request.slot_time is not None
    assert request.slot_time.isoformat() == "2026-10-01T08:30:00+01:00"


def test_scheduling_needs_the_schedule_right(kernel: Kernel, clock: Clock) -> None:
    provision(kernel.database, document(GRANTS), kernel.operations)
    response = put(TestClient(create_app(kernel)), routine_spec())
    assert response.status_code == 403, response.text


def test_supervisors_schedule_only_their_own_work(kernel: Kernel, clock: Clock) -> None:
    response = put(TestClient(create_app(kernel)), routine_spec(), token="outsider")
    assert response.status_code == 403
    assert response.json()["code"] == "trigger_supervisor"


def test_template_is_checked_against_the_live_ceiling(kernel: Kernel, clock: Clock) -> None:
    provision(
        kernel.database,
        document((*RIGHTS, "models.invoke", "listings.search", "tasks.schedule")),
        kernel.operations,
    )
    response = put(TestClient(create_app(kernel)), routine_spec())
    assert response.status_code == 403, response.text


def test_provisioning_keeps_supervisor_triggers_and_owners_are_exclusive(
    kernel: Kernel, clock: Clock
) -> None:
    client = TestClient(create_app(kernel))
    assert put(client, routine_spec()).status_code == 200
    provision(kernel.database, document(), kernel.operations)
    listed = client.get("/v0.2/triggers", headers=HEADERS).json()["triggers"]
    assert [t["trigger_id"] for t in listed if not t["removed"]] == ["loft-search"]
    with pytest.raises(Fault) as owned:
        provision(kernel.database, document(triggers=(routine_spec(),)), kernel.operations)
    assert owned.value.code == "trigger_owner"


def test_supervisor_removes_only_its_own_triggers(kernel: Kernel, clock: Clock) -> None:
    client = TestClient(create_app(kernel))
    assert put(client, routine_spec()).status_code == 200
    outsider = client.delete(
        "/v0.2/triggers/loft-search",
        headers={"Authorization": "Bearer outsider", "X-Cardea-Auth": "spiffe"},
    )
    assert outsider.status_code == 404
    assert client.delete("/v0.2/triggers/loft-search", headers=HEADERS).status_code == 200
    clock.now = datetime(2026, 10, 1, 7, 30, 30, tzinfo=UTC)
    assert Scheduler(kernel, clock).tick() == {}
    host = routine_spec(trigger_id="host-owned")
    provision(kernel.database, document(triggers=(host,)), kernel.operations)
    refused = client.delete("/v0.2/triggers/host-owned", headers=HEADERS)
    assert refused.status_code == 409


def test_trigger_templates_carry_context_in_their_digest(kernel: Kernel, clock: Clock) -> None:
    client = TestClient(create_app(kernel))
    assert put(client, routine_spec()).status_code == 200
    context = [{"operation": "listings.search", "arguments": {"body": "lofts"}}]
    with_context = routine_spec(template=template(context=context))
    assert put(client, with_context).json()["change"] == "updated"
    clock.now = datetime(2026, 10, 1, 7, 30, 30, tzinfo=UTC)
    assert Scheduler(kernel, clock).tick() == {"loft-search": "admitted"}
    with kernel.database.read() as conn:
        request_json = conn.exec_driver_sql("SELECT request_json FROM task_bindings").scalar_one()
    request = AgentRunRequest.model_validate_json(request_json)
    assert [c.model_dump() for c in request.context] == context
    effect = routine_spec(template=template(context=[{"operation": "mail.send"}]))
    refused = put(client, effect)
    assert refused.status_code == 422 and refused.json()["code"] == "context_not_read_only"


def test_trigger_templates_obey_the_configured_task_request_cap(
    kernel: Kernel, clock: Clock
) -> None:
    long = routine_spec(template=template(instructions="Follow the house style. " * 1300))
    client = TestClient(create_app(kernel, max_request_bytes=131072))
    refused = put(client, long)
    assert refused.status_code == 413 and refused.json()["code"] == "request_too_large"
    with pytest.raises(Fault) as provisioned:
        provision(kernel.database, document(triggers=(long,)), kernel.operations)
    assert provisioned.value.code == "request_too_large"
    # The host owner raises the cap: supervisor and host-owned triggers both install and admit.
    kernel.max_task_request_bytes = 65536
    assert put(client, long).status_code == 200
    host = long.model_copy(update={"trigger_id": "host-long"})
    provision(kernel.database, document(triggers=(host,)), kernel.operations, 65536)
    clock.now = datetime(2026, 10, 1, 7, 30, 30, tzinfo=UTC)
    assert Scheduler(kernel, clock).tick() == {"host-long": "admitted", "loft-search": "admitted"}


# Expectations


def test_expectations_must_name_exposed_operations() -> None:
    with pytest.raises(ValidationError):
        run_request(expect=[{"operation": "mail.other"}])


def test_unmet_expectation_retries_then_succeeds(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    async def forgets(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert "exactly 1 successful call(s) of mail-send" in request.prompt
        await call(client, "listings-search")
        return SessionResult(text="Found 3 listings.", is_error=False)

    session.scripts = [forgets, routine]
    task_id = submit(kernel, expect=[{"operation": "mail.send", "min_calls": 1, "max_calls": 1}])
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1


def test_unmet_expectation_fails_after_last_attempt(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    async def forgets(client: ClientSession, request: SessionRequest) -> SessionResult:
        return SessionResult(text="Nothing to do.", is_error=False)

    session.scripts = [forgets, forgets]
    task_id = submit(kernel, expect=[{"operation": "mail.send"}])
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert cast(Any, tx.get_task(task_id).failure).code == "expectation_unmet"


def test_exceeded_expectation_fails_immediately(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    async def double(client: ClientSession, request: SessionRequest) -> SessionResult:
        await call(client, "mail-send", to="a@example.invalid", body="one")
        await call(client, "mail-send", to="b@example.invalid", body="two")
        return SessionResult(text="Sent two.", is_error=False)

    session.scripts = [double]
    task_id = submit(kernel, expect=[{"operation": "mail.send", "max_calls": 1}])
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert cast(Any, tx.get_task(task_id).failure).code == "expectation_exceeded"


# Run evidence


def test_run_view_shows_calls_output_and_evidence(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    session.scripts = [routine]
    task_id = submit(kernel, expect=[{"operation": "mail.send"}])
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    client = TestClient(create_app(kernel))
    view = client.get(f"/v0.2/tasks/{task_id}/run", headers=HEADERS).json()
    assert view["state"] == "succeeded" and view["attempt"] == 1
    assert [c["operation"] for c in view["calls"]] == ["listings.search", "mail.send"]
    with_context = submit(
        kernel, request_id="with-context", context=[{"operation": "listings.search"}]
    )
    session.scripts = [routine]
    assert AgentRunRuntime(kernel).execute(with_context) == "succeeded"
    view_with_context = client.get(f"/v0.2/tasks/{with_context}/run", headers=HEADERS).json()
    assert [c["operation_id"] for c in view_with_context["calls"]] == [
        "ctx-01",
        "call-0001",
        "call-0002",
    ]
    assert [c["operation_id"] for c in view_with_context["output"]["calls"]] == [
        "ctx-01",
        "call-0001",
        "call-0002",
    ]
    assert view["calls"][1]["arguments"]["to"] == "user@example.invalid"
    assert view["output"]["text"] == "Sent the recap."
    assert view["verification"]["expectations"] == ["at least 1 successful call(s) of mail-send"]
    outsider = client.get(
        f"/v0.2/tasks/{task_id}/run",
        headers={"Authorization": "Bearer outsider", "X-Cardea-Auth": "spiffe"},
    )
    assert outsider.status_code == 404


# Typed responses keep the wire shape


def test_typed_responses_match_what_the_handlers_return(
    kernel: Kernel,
    tools: Tools,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every JSON route's response model validates real handler output and serializes it to
    exactly the JSON the untyped handler produced."""
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)

    async def fails_a_read(client: ClientSession, request: SessionRequest) -> SessionResult:
        await call(client, "listings-search", to="upstream-503")
        return await routine(client, request)

    async def sends(client: ClientSession, request: SessionRequest) -> SessionResult:
        await call(client, "mail-send", to="other@example.invalid", body="recap")
        return SessionResult(text="Tried to send.", is_error=False)

    session.scripts = [fails_a_read, sends]
    done = submit(kernel, expect=[{"operation": "mail.send"}])
    assert AgentRunRuntime(kernel).execute(done) == "succeeded"
    tools.crash = True
    uncertain = submit(kernel, request_id="uncertain-send")
    assert AgentRunRuntime(kernel).execute(uncertain) == "failed"

    app = create_app(kernel)
    endpoints = {
        (route.path, method): route.endpoint
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    proof = kernel.auth.validate("supervisor")
    client = TestClient(app)

    def same(path: str, model: type[BaseModel], raw: Any, **params: Any) -> dict[str, Any]:
        """The HTTP body equals the untyped handler's JSON and validates as `model`."""
        body = client.get(path, headers=HEADERS, params=params)
        assert body.status_code == 200, body.text
        document: dict[str, Any] = body.json()
        assert document == jsonable_encoder(raw), path
        assert model.model_validate(document).model_dump(mode="json") == document, path
        return document

    v = "/v0.2"
    same(f"{v}/system", SystemView, endpoints[(f"{v}/system", "GET")](proof))
    same(
        f"{v}/operations",
        OperationCatalog,
        endpoints[(f"{v}/operations", "GET")](proof, "test"),
        namespace="test",
    )
    same(
        f"{v}/host-budget",
        HostBudgetView,
        endpoints[(f"{v}/host-budget", "GET")](proof, "test"),
        namespace="test",
    )
    for task_id in (done, uncertain):
        for suffix, model in (
            ("run", RunView),
            ("receipt", TaskReceipt),
            ("agent-receipts", AgentReceipts),
            ("children", ChildrenView),
        ):
            path = f"{v}/tasks/{{task_id}}/{suffix}"
            same(path.format(task_id=task_id), model, endpoints[(path, "GET")](task_id, proof))
    output = f"{v}/tasks/{{task_id}}/output"
    same(output.format(task_id=done), RunOutput, endpoints[(output, "GET")](done, proof))
    run = same(
        f"{v}/tasks/{done}/run",
        RunView,
        endpoints[(f"{v}/tasks/{{task_id}}/run", "GET")](done, proof),
    )
    assert run["calls"][0]["failure_details"] == {
        "reason": "upstream_http_error",
        "summary": "UpstreamError (HTTP 503)",
    }
    assert isinstance(RunView.model_validate(run).output, AgentRunOutput)

    # Triggers: the mutating routes are compared on their second, idempotent call.
    spec = routine_spec()
    installed = endpoints[(f"{v}/triggers/{{trigger_id}}", "PUT")](spec.trigger_id, spec, proof)
    assert installed == {
        "schema_version": "0.2",
        "trigger_id": "loft-search",
        "change": "installed",
    }
    again = put(client, spec).json()
    assert again == {**installed, "change": "unchanged"}
    TriggerScheduled.model_validate(again)
    same(
        f"{v}/triggers",
        TriggerList,
        endpoints[(f"{v}/triggers", "GET")](proof, after="", limit=20, history=20),
    )
    removed = endpoints[(f"{v}/triggers/{{trigger_id}}", "DELETE")](spec.trigger_id, proof)
    repeated = client.delete(f"{v}/triggers/{spec.trigger_id}", headers=HEADERS).json()
    assert repeated == jsonable_encoder(removed)
    TriggerRemoved.model_validate(repeated)

    # Reconcile: settle the uncertain send through the route.
    async def found(arguments: dict[str, Any], context: Any) -> Reconciliation:
        return Reconciliation(applied=True, output={"message_id": "<found@test>"})

    kernel.operations = Registry(
        (
            descriptor("mail.send", "effect", idempotency_scope=["/to"], reconciler="test.found"),
            descriptor("listings.search", "read"),
        ),
        {"test.mail.send": tools.send, "test.listings.search": tools.search, "test.found": found},
    )
    settled = client.post(
        f"{v}/tasks/{uncertain}/operations/call-0001/reconcile", headers=HEADERS
    ).json()
    assert settled == {"schema_version": "0.2", "state": "succeeded"}
    ReconcileResult.model_validate(settled)
