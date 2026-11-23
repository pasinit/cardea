"""The content gate in an agent run (ADR-037): a refused tool call leaves no copy.

The path the dev team uses: a session's tool call is refused by the gate, the attempt
fails, a retried attempt follows and the run finishes. The test then scans every file
under the store's directory (SQLite, its log, objects, attempt logs), the logs, the mail
and the retried attempt's prompt for the refused content.
"""

import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import test_agent_runs as runs
from mcp.client.session import ClientSession
from pydantic import JsonValue
from test_content_gate import DIFF, FAKE_KEY, GITLEAKS, TERM, config, leaves, private

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, approval_requests
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentRunRequest,
    AttemptLogging,
    Permission,
    Principal,
    Provisioning,
    now,
)
from cardea.service.operations import OperationContext, OperationDescriptor, Registry

PUSH = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "vcs.push_branch",
            "description": "A fake push: sends the change after the content gate.",
            "inputSchema": {
                "type": "object",
                "properties": {"diff": {"type": "string"}, "message": {"type": "string"}},
                "required": ["diff", "message"],
            },
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.push"},
    }
)
GRANTS = (*RIGHTS, "models.invoke", "vcs.push_branch")


class Remote:
    def __init__(self) -> None:
        self.pushed: list[dict[str, JsonValue]] = []

    async def push(self, arguments: dict[str, JsonValue], context: OperationContext) -> Any:
        assert context.check_content is not None
        context.check_content(
            operation="vcs.push_branch",
            diff=str(arguments["diff"]),
            texts={"commit_message": str(arguments["message"])},
        )
        self.pushed.append(arguments)
        return {"pushed": True}


@pytest.fixture
def remote() -> Remote:
    return Remote()


@pytest.fixture
def session() -> runs.FakeSession:
    return runs.FakeSession()


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: Remote, session: runs.FakeSession
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", runs.FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    registry = Registry((PUSH,), {"test.push": remote.push})
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
        for right in GRANTS
    )
    provision(
        db,
        Provisioning(
            principals=tuple(
                Principal(
                    principal_id=p,
                    kind="service" if p == "runtime" else "human",
                    auth_kind="spiffe",
                    issuer="test",
                    subject=p,
                )
                for p in ("runtime", "supervisor")
            )
            + (Principal(principal_id="coder", kind="agent", runtime_id="runtime"),),
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(db, cast(Authentication, runs.Auth()), operations=registry, session=session)
    # Attempt logs go to files under the store, so the scan covers them too.
    instance.attempt_logging = AttemptLogging(files=True)
    try:
        yield instance
    finally:
        db.close()


@pytest.mark.parametrize("leak", ["denylist", "secret"])
@pytest.mark.loopback
def test_a_refused_tool_call_leaves_no_copy_across_a_retry(
    kernel: Kernel,
    remote: Remote,
    session: runs.FakeSession,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    leak: str,
) -> None:
    if leak == "secret" and not GITLEAKS:
        pytest.skip("set CARDEA_TEST_GITLEAKS to the pinned Gitleaks binary")
    kernel.content_gate = config(
        tmp_path, real=True, denylist_file=str(private(tmp_path / "deny", f"{TERM}\n"))
    )
    needle = TERM if leak == "denylist" else FAKE_KEY
    leaked = DIFF.replace('+REGION = "eu-west-1"', f"+NOTE = '{needle}'")
    replies: list[str] = []

    async def pushes(client: ClientSession, request: SessionRequest) -> SessionResult:
        refused = await runs.call(client, "vcs-push_branch", diff=leaked, message=f"Ship {needle}")
        assert refused.is_error
        replies.append(str(refused.content))
        return SessionResult(text="", is_error=True, stop_reason="error_max_turns")

    async def finishes(client: ClientSession, request: SessionRequest) -> SessionResult:
        return SessionResult(text="The push was refused; I stopped.", is_error=False)

    caplog.set_level("DEBUG")
    session.scripts = [pushes, finishes]
    request = AgentRunRequest.model_validate(
        {
            "request_id": f"push-run-{leak}",
            "agent_id": "coder",
            "namespace": "test",
            "objective": "Push a change",
            "instructions": "Push the change.",
            "operations": ["vcs.push_branch"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
            "max_attempts": 2,
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert remote.pushed == []
    rule = "denylist:1" if leak == "denylist" else "aws-access-token"
    # The agent was told the rule and the location, never the content.
    assert f"{rule} at app/settings.py:3" in replies[0] and needle not in replies[0]
    # The retried attempt's prompt shows the call as refused, with rule and location only.
    retried = session.requests[1].prompt
    assert "This is attempt 2" in retried and needle not in retried
    assert "content_refused" in retried and f"{rule} at app/settings.py:3" in retried
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "call-0001")
    assert receipt.redacted()
    with kernel.database.read() as conn:
        (mail,) = conn.exec_driver_sql("SELECT parts_json FROM mailbox_messages").one()
    assert needle not in mail and rule in mail
    kernel.database.close()
    stored = leaves(tmp_path / "data")
    assert any(p.name.endswith(".log") for p in (tmp_path / "data").rglob("*"))
    assert needle.encode() not in stored and needle.casefold().encode() not in stored.lower()
    # The kernel's logs, its tool endpoint's included. The fake agent's own MCP client
    # logs what it sent at DEBUG; that is the harness's side, not the kernel's.
    leaking = {
        r.name
        for r in caplog.records
        if needle in r.getMessage() and not r.name.startswith("mcp.client")
    }
    assert not leaking, leaking


@pytest.mark.loopback
def test_a_call_held_for_approval_and_refused_as_apr_leaves_no_copy(
    kernel: Kernel, remote: Remote, session: runs.FakeSession, tmp_path: Path
) -> None:
    """Held for approval (ADR-036), approved, then refused by the gate when it runs."""
    expires = (now() + timedelta(hours=1)).isoformat()
    people = ("runtime", "supervisor", "approver")
    provision(
        kernel.database,
        Provisioning.model_validate(
            {
                "principals": [
                    {
                        "principal_id": p,
                        "kind": "service" if p == "runtime" else "human",
                        "auth_kind": "spiffe",
                        "issuer": "test",
                        "subject": p,
                    }
                    for p in people
                ]
                + [{"principal_id": "coder", "kind": "agent", "runtime_id": "runtime"}],
                "permissions": [
                    {
                        "principal_id": "supervisor",
                        "namespace": "test",
                        "operation": right,
                        "delegate": True,
                        "expires_at": expires,
                    }
                    for right in GRANTS
                ]
                + [
                    {
                        "principal_id": "approver",
                        "namespace": "test",
                        "operation": "vcs.push_branch",
                        "allow": False,
                        "approve": True,
                        "expires_at": expires,
                    }
                ],
                "platform": [
                    {
                        "principal_id": "platform",
                        "namespace": "test",
                        "operation": right,
                        "approve": right == "vcs.push_branch",
                        "expires_at": expires,
                    }
                    for right in GRANTS
                ],
                "approvals": {
                    "entries": [
                        {
                            "namespace": "test",
                            "operation": "vcs.push_branch",
                            "rules": [
                                {"rule_id": "push", "level": "always", "reason": "Pushes wait"}
                            ],
                        }
                    ]
                },
            }
        ),
        kernel.operations,
    )
    kernel.content_gate = config(
        tmp_path, denylist_file=str(private(tmp_path / "deny", f"{TERM}\n"))
    )
    leaked = DIFF.replace('+REGION = "eu-west-1"', f"+NOTE = '{TERM}'")

    async def asks(client: ClientSession, request: SessionRequest) -> SessionResult:
        held = await runs.call(client, "vcs-push_branch", diff=leaked, message="Ship it")
        assert held.is_error and "approval_pending" in str(held.content)
        return SessionResult(text="Waiting for approval.", is_error=False)

    resumed: list[str] = []

    async def told(client: ClientSession, request: SessionRequest) -> SessionResult:
        resumed.append(request.prompt)
        return SessionResult(text="The approved push was refused; I stopped.", is_error=False)

    session.scripts = [asks, told]
    request = AgentRunRequest.model_validate(
        {
            "request_id": "held-push",
            "agent_id": "coder",
            "namespace": "test",
            "objective": "Push a change",
            "instructions": "Push the change.",
            "operations": ["vcs.push_branch"],
            "grants": list(GRANTS),
            "attempt_timeout_seconds": 30,
            "max_attempts": 2,
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    assert AgentRunRuntime(kernel).execute(task_id) == "waiting"
    with kernel.database.read() as conn:
        row = dict(
            conn.exec_driver_sql(
                "SELECT request_id,intent_digest FROM approval_requests WHERE task_id=?",
                (task_id,),
            )
            .mappings()
            .one()
        )
    approval_requests.decide(
        kernel,
        Proof("spiffe", "test", "approver", time.time() + 60),
        str(row["request_id"]),
        approval_requests.DecisionRequest.model_validate(
            {"decision_id": "d-1", "decision": "approve", "intent_digest": row["intent_digest"]}
        ),
    )
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert remote.pushed == []
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "apr-01")
        frozen = conn.exec_driver_sql(
            "SELECT arguments_json,intent_digest FROM approval_requests WHERE task_id=?",
            (task_id,),
        ).one()
    assert receipt.redacted() and frozen[1] == row["intent_digest"]
    assert '"redacted": "content_refused"' in frozen[0]
    assert "content_refused" in resumed[0] and TERM not in resumed[0]
    kernel.database.close()
    stored = leaves(tmp_path / "data")
    assert TERM.encode() not in stored and TERM.casefold().encode() not in stored.lower()
