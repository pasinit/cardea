"""The content gate for confined workers (ADR-037, ADR-028): a kernel callback.

The fake root helper runs the real worker loop (`operation_worker.run`) in process, so the
worker's `check_content` reaches the kernel's per-call loopback endpoint over real HTTP.
The gate's files never appear in what the worker is given.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue
from test_confined import WorkerHost, finished
from test_content_gate import DIFF, TERM, config, leaves, private
from test_operations import Auth, FakeClient

from cardea.hosts import operation_worker
from cardea.kernel.models import OperationState
from cardea.service import confined, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.auth import Authentication
from cardea.service.confined import Workers
from cardea.service.content_gate import remote_check
from cardea.service.database import Database, borrow
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
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Registry,
)

PUSH = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "vcs.push_branch",
            "inputSchema": {
                "type": "object",
                "properties": {"diff": {"type": "string"}, "message": {"type": "string"}},
                "required": ["diff", "message"],
            },
            "outputSchema": {"type": "object"},
        },
        "cardea": {
            "version": 1,
            "effect": "effect",
            "executor": {"kind": "confined", "profile": "browser", "worker": "post"},
            "timeout_seconds": 2,
        },
    }
)


class Sandbox(WorkerHost):
    """Plays the root helper and runs the worker loop, like the sandboxed process."""

    def __init__(self) -> None:
        super().__init__()
        self.pushed: list[dict[str, JsonValue]] = []
        self.ignore_refusal = False

    async def push(self, arguments: dict[str, JsonValue], context: OperationContext) -> Any:
        assert context.check_content is not None
        try:
            context.check_content(
                operation="vcs.push_branch",
                diff=str(arguments["diff"]),
                texts={"commit_message": str(arguments["message"])},
            )
        except OperationRejected:
            if not self.ignore_refusal:
                raise
        self.pushed.append(arguments)
        return {"pushed": True}

    def start_confined(
        self, job: str, content: bytes, deadline: float, profile: str, worker: str
    ) -> None:
        super().start_confined(job, content, deadline, profile, worker)
        self.outcome = finished(operation_worker.run(self.push, json.loads(content)))


@pytest.fixture
def sandbox() -> Sandbox:
    return Sandbox()


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: Sandbox) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    monkeypatch.setattr(confined, "POLL_SECONDS", 0.01)
    registry = Registry((PUSH,), {})
    database = Database(tmp_path / "data")
    database.migrate()
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*RIGHTS, "vcs.push_branch")
    )
    provision(
        database,
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
            + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),),
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
    )
    instance = Kernel(
        database, cast(Authentication, Auth()), operations=registry, workers=Workers(sandbox)
    )
    try:
        yield instance
    finally:
        database.close()


def push(kernel: Kernel, diff: str, message: str = "Tidy settings") -> tuple[str, Any]:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": "push-1",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Push a change",
            "operation": "vcs.push_branch",
            "arguments": {"diff": diff, "message": message},
            "grants": ["objects.read", "objects.write", "vcs.push_branch"],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    state = OperationRuntime(kernel).execute(task_id)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        return state, tx.get_operation(task_id, "execute")


def gate(kernel: Kernel, tmp_path: Path, **changes: Any) -> Path:
    denylist = private(tmp_path / "deny", f"{TERM}\n")
    kernel.content_gate = config(tmp_path, denylist_file=str(denylist), **changes)
    return denylist


@pytest.mark.loopback
def test_a_clean_change_passes_the_gate_from_inside_the_sandbox(
    kernel: Kernel, sandbox: Sandbox, tmp_path: Path
) -> None:
    denylist = gate(kernel, tmp_path)
    state, _ = push(kernel, DIFF)
    assert state == "succeeded" and len(sandbox.pushed) == 1
    # The worker got a loopback callback and its token; never the gate's files.
    (start,) = sandbox.starts
    callback = start["payload"]["context"]["content_gate"]
    assert callback["url"].startswith("http://127.0.0.1:") and len(callback["token"]) >= 32
    assert kernel.content_gate is not None
    given = json.dumps(start["payload"])
    assert str(denylist) not in given and str(kernel.content_gate.gitleaks_binary) not in given


@pytest.mark.loopback
def test_a_refusal_inside_the_sandbox_is_the_same_refusal(
    kernel: Kernel, sandbox: Sandbox, tmp_path: Path
) -> None:
    gate(kernel, tmp_path)
    state, receipt = push(kernel, DIFF, message=f"Ship {TERM}")
    assert state == "failed" and sandbox.pushed == []
    assert receipt.failure is not None and receipt.failure.code == "content_refused"
    assert "denylist:1 at commit_message" in str(receipt.failure.details["summary"])
    # The kernel redacts the receipt as for an in-process operation; nothing keeps the term.
    assert receipt.redacted()
    kernel.database.close()
    assert TERM.encode() not in leaves(tmp_path / "data")


def test_a_worker_that_ignores_a_refusal_leaves_the_effect_in_doubt(
    kernel: Kernel, sandbox: Sandbox, tmp_path: Path
) -> None:
    gate(kernel, tmp_path)
    sandbox.ignore_refusal = True
    state, receipt = push(kernel, DIFF, message=f"Ship {TERM}")
    assert state == "failed" and receipt.state is OperationState.OUTCOME_UNKNOWN


@pytest.mark.loopback
def test_without_a_gate_the_sandboxed_call_sends_nothing(kernel: Kernel, sandbox: Sandbox) -> None:
    state, receipt = push(kernel, DIFF)
    assert state == "failed" and sandbox.pushed == []
    assert receipt.failure is not None and receipt.failure.code == "content_gate_unavailable"


@pytest.mark.loopback
def test_a_worker_without_a_callback_refuses_to_send() -> None:
    for callback in (None, {"url": "http://127.0.0.1:9/check", "token": "x"}):
        with pytest.raises(OperationRejected) as refused:
            remote_check(callback, timeout=1)(operation="vcs.push_branch", diff=DIFF)
        assert refused.value.code == "content_gate_unavailable"
