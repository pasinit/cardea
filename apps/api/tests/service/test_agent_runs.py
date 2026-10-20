"""Agent runs (ADR-027): sessions whose only tools are Cardea operations; portable fakes."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import JsonValue, ValidationError

from cardea.kernel.models import OperationState, ResourceRef
from cardea.runtime.agent_session import ClaudeAgentSession, SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentRunRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    Reconciliation,
    Registry,
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


def descriptor(name: str, effect: str, **binding: Any) -> OperationDescriptor:
    return OperationDescriptor.model_validate(
        {
            "tool": {
                "name": name,
                "description": f"{name} for tests",
                "inputSchema": {
                    "type": "object",
                    "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                },
                "outputSchema": {"type": "object"},
            },
            "cardea": {
                "version": 1,
                "effect": effect,
                "handler": f"test.{name}",
                "timeout_seconds": 2,
                **binding,
            },
        }
    )


class Tools:
    def __init__(self) -> None:
        self.sent: list[dict[str, JsonValue]] = []
        self.contexts: list[OperationContext] = []
        self.crash = False

    async def send(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        self.sent.append(arguments)
        self.contexts.append(context)
        if self.crash:
            raise ConnectionError("reset after the provider accepted")
        return {"message_id": f"<{context.effect_key[:16]}@test>"}

    async def search(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        return {"listings": 3}


Script = Callable[[ClientSession, SessionRequest], Awaitable[SessionResult]]


class FakeSession:
    """Plays a scripted agent over the real per-attempt MCP endpoint."""

    def __init__(self) -> None:
        self.scripts: list[Script] = []
        self.requests: list[SessionRequest] = []

    async def run(self, request: SessionRequest) -> SessionResult:
        self.requests.append(request)
        script = self.scripts.pop(0)
        headers = {"Authorization": f"Bearer {request.mcp_token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(request.mcp_url, http_client=http) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            return await script(session, request)


async def call(session: ClientSession, name: str, **arguments: Any) -> types.CallToolResult:
    return await session.call_tool(name, arguments)


GRANTS = (*RIGHTS, "models.invoke", "mail.send", "listings.search")


@pytest.fixture
def tools() -> Tools:
    return Tools()


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


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
            expires_at=now() + timedelta(hours=1),
        )
        for right in GRANTS
    )
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry, session=session)
    try:
        yield instance
    finally:
        db.close()


def run_request(**changes: Any) -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "request_id": "routine-0830",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Daily listings recap",
            "instructions": "Search listings, then email the user one recap.",
            "operations": ["listings.search", "mail.send"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
            **changes,
        }
    )


def submit(kernel: Kernel, **changes: Any) -> str:
    return kernel.submit(kernel.auth.validate("supervisor"), run_request(**changes)).task.task_id


def calls(kernel: Kernel, task_id: str) -> list[Any]:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return tx.list_operations(task_id)


async def routine(session: ClientSession, request: SessionRequest) -> SessionResult:
    listed = await session.list_tools()
    assert {tool.name for tool in listed.tools} == {"listings-search", "mail-send"}
    found = await call(session, "listings-search")
    assert not found.is_error and found.structured_content == {"listings": 3}
    sent = await call(session, "mail-send", to="user@example.invalid", body="3 new listings")
    assert not sent.is_error, sent.content
    return SessionResult(text="Sent the recap.", is_error=False, cost_usd=0.01, turns=3)


# Admission


def test_admission_needs_model_and_operation_grants(kernel: Kernel) -> None:
    with pytest.raises(ValidationError):
        run_request(grants=[*RIGHTS, "mail.send", "listings.search"])
    with pytest.raises(ValidationError):
        run_request(grants=[*RIGHTS, "models.invoke", "mail.send"])


def test_admission_requires_a_session_runtime(kernel: Kernel) -> None:
    kernel.session = None
    with pytest.raises(Fault) as disabled:
        submit(kernel)
    assert disabled.value.code == "agent_disabled"


def test_admission_requires_installed_operations(kernel: Kernel) -> None:
    kernel.operations = Registry(
        (descriptor("listings.search", "read"),), {"test.listings.search": Tools().search}
    )
    with pytest.raises(Fault) as missing:
        submit(kernel)
    assert missing.value.code == "operation_not_installed"


# Execution


def test_every_tool_call_is_a_receipted_operation(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    session.scripts = [routine]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1
    receipts = calls(kernel, task_id)
    assert [(r.request.operation_id, r.request.name, r.state) for r in receipts] == [
        ("call-0001", "listings.search", OperationState.SUCCEEDED),
        ("call-0002", "mail.send", OperationState.SUCCEEDED),
    ]
    request = session.requests[0]
    assert request.tool_names == ("listings-search", "mail-send")
    assert "Search listings" in request.prompt
    assert "The current time is 20" in request.system_prompt
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        import json

        output = tx.latest_object(ResourceRef(namespace="test", resource_id=f"{task_id}.output"))
        document = json.loads(tx.read_object(output))
        assert document["text"] == "Sent the recap." and document["attempts"] == 1
        assert tx.get_task(task_id).verification is not None


def test_agent_receipts_list_the_run_calls(kernel: Kernel, session: FakeSession) -> None:
    session.scripts = [routine]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    with TestClient(create_app(kernel)) as client:
        headers = {"Authorization": "Bearer supervisor"}
        receipts = client.get(f"/v0.2/tasks/{task_id}/agent-receipts", headers=headers).json()
        output = client.get(f"/v0.2/tasks/{task_id}/output", headers=headers).json()
        outsider = client.get(
            f"/v0.2/tasks/{task_id}/agent-receipts", headers={"Authorization": "Bearer outsider"}
        )
    listed = receipts["receipts"]
    assert [(r["request"]["operation_id"], r["request"]["name"], r["state"]) for r in listed] == [
        (c["operation_id"], c["operation"], c["state"]) for c in output["content"]["calls"]
    ]
    search, send = listed
    assert search["retry_class"] == "read_only"
    assert send["retry_class"] == "reconciliation_required"
    assert send["request"]["arguments"]["scope_key"] is not None
    assert outsider.status_code == 404


def test_endpoint_requires_the_attempt_token(kernel: Kernel, session: FakeSession) -> None:
    async def wrong_token(client: ClientSession, request: SessionRequest) -> SessionResult:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer nope"}) as http:
            response = await http.post(request.mcp_url, json={})
        assert response.status_code == 401
        return SessionResult(text="done", is_error=False)

    session.scripts = [wrong_token]
    assert AgentRunRuntime(kernel).execute(submit(kernel)) == "succeeded"


def test_tool_budget_is_enforced(kernel: Kernel, tools: Tools, session: FakeSession) -> None:
    async def greedy(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert not (await call(client, "listings-search")).is_error
        refused = await call(client, "listings-search")
        assert refused.is_error
        assert "tool_budget_exhausted" in cast(types.TextContent, refused.content[0]).text
        return SessionResult(text="stopped", is_error=False)

    session.scripts = [greedy]
    assert AgentRunRuntime(kernel).execute(submit(kernel, max_tool_calls=1)) == "succeeded"


def test_failed_attempt_retries_with_journal_and_does_not_resend(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    async def sends_then_fails(client: ClientSession, request: SessionRequest) -> SessionResult:
        await call(client, "mail-send", to="user@example.invalid", body="3 new listings")
        return SessionResult(text="", is_error=True, stop_reason="error_max_turns")

    async def resends(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert "attempt 2" in request.prompt and "mail-send" in request.prompt
        again = await call(client, "mail-send", to="user@example.invalid", body="Three listings")
        assert not again.is_error
        assert "Already done" in cast(types.TextContent, again.content[0]).text
        return SessionResult(text="Recap was already sent.", is_error=False)

    session.scripts = [sends_then_fails, resends]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1
    receipts = calls(kernel, task_id)
    assert receipts[1].result is not None and receipts[1].result["deduplicated"] is True


def test_attempts_are_bounded(kernel: Kernel, session: FakeSession) -> None:
    async def fails(client: ClientSession, request: SessionRequest) -> SessionResult:
        return SessionResult(text="", is_error=True, stop_reason="error_during_execution")

    session.scripts = [fails, fails]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert cast(Any, tx.get_task(task_id).failure).code == "session_failed"


def test_uncertain_effect_fences_the_run(
    kernel: Kernel, tools: Tools, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    tools.crash = True

    async def sends(client: ClientSession, request: SessionRequest) -> SessionResult:
        result = await call(client, "mail-send", to="user@example.invalid", body="recap")
        assert result.is_error
        assert "outcome_unknown" in cast(types.TextContent, result.content[0]).text
        return SessionResult(text="Tried to send.", is_error=False)

    session.scripts = [sends]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert cast(Any, tx.get_task(task_id).failure).code == "reconciliation_required"
        assert tx.get_operation(task_id, "call-0001").state is OperationState.OUTCOME_UNKNOWN


def test_reconciler_sees_the_original_call_context(
    kernel: Kernel, tools: Tools, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[OperationContext] = []

    async def reconcile(
        arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        seen.append(context)
        return Reconciliation(applied=True, output={"message_id": "<found@test>"})

    kernel.operations = Registry(
        (
            descriptor(
                "mail.send", "effect", idempotency_scope=["/to"], reconciler="test.reconcile"
            ),
            descriptor("listings.search", "read"),
        ),
        {
            "test.mail.send": tools.send,
            "test.listings.search": tools.search,
            "test.reconcile": reconcile,
        },
    )
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    tools.crash = True

    async def sends(client: ClientSession, request: SessionRequest) -> SessionResult:
        result = await call(client, "mail-send", to="user@example.invalid", body="recap")
        assert "outcome_unknown" in cast(types.TextContent, result.content[0]).text
        return SessionResult(text="Tried to send.", is_error=False)

    session.scripts = [sends]
    slot = {
        "idempotency_context": "daily.20261020T073000Z",
        "slot_time": "2026-10-20T08:30:00+01:00",
    }
    task_id = submit(kernel, **slot)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert calls(kernel, task_id)[0].state is OperationState.OUTCOME_UNKNOWN
    assert OperationRuntime(kernel).reconcile(task_id, "call-0001") == "succeeded"
    (original,) = tools.contexts
    (reconciling,) = seen
    for field in ("agent_id", "idempotency_context", "slot_time", "effect_key", "task_id"):
        assert getattr(reconciling, field) == getattr(original, field), field
    assert reconciling.agent_id == "agent"
    assert reconciling.slot_time == "2026-10-20T08:30:00+01:00"


def test_crash_after_effect_resumes_without_resending(
    kernel: Kernel, tools: Tools, session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    # With the default 30 s margin a 4 s lease leaves the session no time at all, so the
    # retry passed only when it finished within one poll; give it the lease's 3 s.
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)

    class Crash(Exception):
        pass

    def crash(stage: str) -> None:
        if stage == "agent_run.before_finish":
            raise Crash

    async def resends(client: ClientSession, request: SessionRequest) -> SessionResult:
        again = await call(client, "mail-send", to="user@example.invalid", body="recap again")
        assert "Already done" in cast(types.TextContent, again.content[0]).text
        return SessionResult(text="Done.", is_error=False)

    session.scripts = [routine, resends]
    task_id = submit(kernel)
    kernel.fault = crash
    with pytest.raises(Crash):
        AgentRunRuntime(kernel).execute(task_id)
    kernel.fault = lambda stage: None
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(tools.sent) == 1


def test_orphaned_session_cannot_act(kernel: Kernel, tools: Tools, session: FakeSession) -> None:
    leases: list[str] = []

    async def remember(client: ClientSession, request: SessionRequest) -> SessionResult:
        with kernel.database.engine.begin() as conn, borrow(conn) as tx:
            lease = tx.get_task(task_id).lease
            assert lease is not None
            leases.append(lease.lease_id)
        return SessionResult(text="", is_error=True, stop_reason="error_max_turns")

    async def finish(client: ClientSession, request: SessionRequest) -> SessionResult:
        stale = await AgentRunRuntime(kernel).call_tool(
            task_id, leases[0], run_request(), "mail.send", {"to": "user@example.invalid"}
        )
        assert stale.is_error
        assert "stale_session" in cast(types.TextContent, stale.content[0]).text
        return SessionResult(text="Done.", is_error=False)

    session.scripts = [remember, finish]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert tools.sent == []


def test_cancellation_stops_the_session(kernel: Kernel, session: FakeSession) -> None:
    async def slow(client: ClientSession, request: SessionRequest) -> SessionResult:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO control_requests VALUES (?,?,?,?)", (task_id, "cancel-1", "cancel", 0)
            )
        await asyncio.sleep(30)
        return SessionResult(text="late", is_error=False)

    session.scripts = [slow]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "cancelled"


def test_revoked_authority_refuses_tool_calls(kernel: Kernel, session: FakeSession) -> None:
    async def revoked(client: ClientSession, request: SessionRequest) -> SessionResult:
        with kernel.database.engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM policies WHERE operation='mail.send'")
        refused = await call(client, "mail-send", to="user@example.invalid", body="x")
        assert refused.is_error
        return SessionResult(text="Could not send.", is_error=False)

    session.scripts = [revoked]
    task_id = submit(kernel)
    AgentRunRuntime(kernel).execute(task_id)
    assert all(r.request.name != "mail.send" for r in calls(kernel, task_id))


# Slots and idempotency contexts


def test_second_task_for_the_same_slot_cannot_repeat_an_effect(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    async def resends(client: ClientSession, request: SessionRequest) -> SessionResult:
        again = await call(client, "mail-send", to="user@example.invalid", body="recap, again")
        assert not again.is_error
        assert "Already done" in cast(types.TextContent, again.content[0]).text
        return SessionResult(text="The recap was already sent.", is_error=False)

    session.scripts = [routine, resends]
    slot = {
        "idempotency_context": "daily.20261020T073000Z",
        "slot_time": "2026-10-20T08:30:00+01:00",
    }
    first = submit(kernel, request_id="routine-0830-a", **slot)
    second = submit(kernel, request_id="routine-0830-b", **slot)
    assert first != second
    runtime = AgentRunRuntime(kernel)
    assert runtime.execute(first) == "succeeded"
    assert runtime.execute(second) == "succeeded"
    assert len(tools.sent) == 1
    duplicate = calls(kernel, second)[0]
    assert duplicate.result is not None and duplicate.result["deduplicated"] is True
    (context,) = tools.contexts
    assert context.agent_id == "agent"
    assert context.idempotency_context == "daily.20261020T073000Z"
    assert context.slot_time == "2026-10-20T08:30:00+01:00"
    assert duplicate.request.arguments["context"] == {
        "agent_id": "agent",
        "idempotency_context": "daily.20261020T073000Z",
        "slot_time": "2026-10-20T08:30:00+01:00",
    }
    assert "Tuesday 2026-10-20 08:30 local time (UTC+0100" in session.requests[0].system_prompt


def test_unscheduled_runs_scope_effects_to_the_task(
    kernel: Kernel, tools: Tools, session: FakeSession
) -> None:
    session.scripts = [routine, routine]
    runtime = AgentRunRuntime(kernel)
    assert runtime.execute(submit(kernel, request_id="ad-hoc-a")) == "succeeded"
    assert runtime.execute(submit(kernel, request_id="ad-hoc-b")) == "succeeded"
    assert len(tools.sent) == 2
    assert all(c.idempotency_context is None and c.slot_time is None for c in tools.contexts)
    assert "scheduled time" not in session.requests[0].system_prompt


# Claude adapter configuration


def test_claude_session_has_no_builtin_tools_or_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "service-secret")
    adapter = ClaudeAgentSession(model="claude-sonnet-5-5", api_key="k")
    options = adapter.options(
        SessionRequest(
            system_prompt="s",
            prompt="p",
            mcp_url="http://127.0.0.1:1/mcp",
            mcp_token="t",
            tool_names=("mail-send",),
            max_turns=5,
            max_cost_usd=1.0,
            timeout_seconds=60,
        ),
        tmp_path,
    )
    assert options["tools"] == []
    assert options["allowed_tools"] == ["mcp__cardea__mail-send"]
    assert options["strict_mcp_config"] is True and options["setting_sources"] == []
    assert options["env"]["HOME"] == str(tmp_path)
    # The SDK merges the service environment underneath; secrets must be blanked.
    assert options["env"]["SMTP_PASSWORD"] == ""
    assert "PATH" not in options["env"]
    assert options["mcp_servers"]["cardea"]["headers"] == {"Authorization": "Bearer t"}
    with pytest.raises(ValueError):
        ClaudeAgentSession(model=None)
