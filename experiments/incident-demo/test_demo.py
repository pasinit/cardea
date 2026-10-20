"""The incident demo: an agent run whose only tool is the installed `incidents.compile`."""

import json
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from cardea.kernel.models import OperationState, ResourceRef
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentRunRequest,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operations import Registry
from incidents import IncidentArtifacts, IncidentBatch, verify
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from operation import compile_incidents, descriptor

BATCH = {
    "records": [
        {"id": "INC-2", "title": "Checkout latency", "severity": "high"},
        {"id": "INC-1", "title": "Stale cache banner", "severity": "low"},
    ]
}
GRANTS = (*RIGHTS, "models.invoke", "incidents.compile")


class DBOSStub:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def enqueue_in_transaction(self, *args: Any) -> None:
        pass

    def destroy(self) -> None:
        pass


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "demo", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        return Proof("spiffe", "demo", token, time.time() + 60)


Script = Callable[[ClientSession, SessionRequest], Awaitable[SessionResult]]


class ScriptedSession:
    """Stands in for the model: plays a script against the run's real MCP endpoint."""

    def __init__(self, *scripts: Script) -> None:
        self.scripts = list(scripts)

    async def run(self, request: SessionRequest) -> SessionResult:
        headers = {"Authorization": f"Bearer {request.mcp_token}"}
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            streamable_http_client(request.mcp_url, http_client=http) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            return await self.scripts.pop(0)(session, request)


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", DBOSStub)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    registry = Registry((descriptor(),), {"incident-demo.compile": compile_incidents})
    database = Database(tmp_path / "data")
    database.migrate()
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="demo",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in GRANTS
    )
    provision(
        database,
        Provisioning(
            principals=(
                Principal(
                    principal_id="runtime",
                    kind="service",
                    auth_kind="spiffe",
                    issuer="demo",
                    subject="runtime",
                ),
                Principal(
                    principal_id="supervisor",
                    kind="human",
                    auth_kind="spiffe",
                    issuer="demo",
                    subject="supervisor",
                ),
                Principal(principal_id="indexer", kind="agent", runtime_id="runtime"),
            ),
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(database, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        database.close()


def submit(kernel: Kernel) -> str:
    request = AgentRunRequest.model_validate(
        {
            "request_id": "index-incidents",
            "agent_id": "indexer",
            "namespace": "demo",
            "objective": "Index the fictional incident records",
            "instructions": "Compile the incident index, then summarize it in one sentence.",
            "operations": ["incidents.compile"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
            "expect": [{"operation": "incidents.compile", "min_calls": 1}],
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


async def indexes(client: ClientSession, request: SessionRequest) -> SessionResult:
    tools = await client.list_tools()
    assert [tool.name for tool in tools.tools] == ["incidents-compile"]
    result = await client.call_tool("incidents-compile", {"batch": BATCH})
    assert not result.is_error, result.content
    # Independent check of observable output, not the model's own judgement.
    verify(
        IncidentBatch.model_validate(BATCH),
        IncidentArtifacts.model_validate(result.structured_content),
    )
    return SessionResult(text="Indexed two incidents.", is_error=False)


def test_agent_run_indexes_incidents_through_the_installed_operation(kernel: Kernel) -> None:
    kernel.session = ScriptedSession(indexes)
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        (receipt,) = tx.list_operations(task_id)
        assert receipt.request.name == "incidents.compile"
        assert receipt.state is OperationState.SUCCEEDED
        output = tx.latest_object(ResourceRef(namespace="demo", resource_id=f"{task_id}.output"))
        assert json.loads(tx.read_object(output))["calls"][0]["operation"] == "incidents.compile"


def test_interrupted_run_resumes_from_its_receipts(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the tool call settled: the next attempt sees the call in its journal."""
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)

    class Crash(Exception):
        pass

    def crash(stage: str) -> None:
        if stage == "agent_run.before_finish":
            raise Crash

    async def summarizes(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert "attempt 2" in request.prompt and "incidents-compile" in request.prompt
        return SessionResult(text="Indexed two incidents earlier.", is_error=False)

    kernel.session = ScriptedSession(indexes, summarizes)
    task_id = submit(kernel)
    kernel.fault = crash
    with pytest.raises(Crash):
        AgentRunRuntime(kernel).execute(task_id)
    kernel.fault = lambda stage: None
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        assert len(tx.list_operations(task_id)) == 1
