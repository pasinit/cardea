"""ADR-034's two worked examples as executable tests (acceptance tests 1 and 2, in part).

Both use only the kernel's generic primitives (collections, records, state machines, claims,
agent groups, approvals) plus a fake operations package. Nothing in this module or the
kernel changes between the software team and the trading desk; only configuration does.

Adapted from the ADR text where later slices are missing:
- event triggers on group pools arrive with slice 7, so pools here use time triggers and the
  tests start each member's run directly;
- approval waits inside agent runs arrive with ADR-036 slice 4, so the desk's gated `approve`
  transition is refused in an agent run (`approval_unsupported`) and is then requested by a
  single-operation task, which waits for the human approver.
"""

import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import test_agent_runs as runs
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from pydantic import JsonValue

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.groups import apply as apply_group
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    OperationTaskRequest,
    Provisioning,
)
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciliation,
    Registry,
)
from cardea.service.records import CollectionDocument, apply_collection

TOKENS = {"runtime", "sup-eng", "sup-desk", "desk-approver"}
RUN = ("objects.read", "objects.write", "models.invoke")


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
        if token not in TOKENS:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


# The operations package: fake version control, CI, risk and broker operations.


class Package:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, JsonValue]] = {}
        self.placements = 0

    async def open_change(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        return {"change_id": f"change-{ctx.receipt_id[-6:]}"}

    async def read(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        return {"ok": True}

    async def place(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        if float(cast(float, arguments["qty"])) > 1000:
            raise OperationRejected("The order would breach the desk limit", "risk_limit")
        self.placements += 1
        order_id = f"B-{ctx.effect_key[:8]}"
        self.orders[str(arguments["order_ref"])] = {"broker_order_id": order_id}
        return {"broker_order_id": order_id}

    async def find(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        found = self.orders.get(str(arguments["order_ref"]))
        return Reconciliation(applied=found is not None, output=found)

    async def fills(self, arguments: dict[str, JsonValue], ctx: OperationContext) -> Any:
        return {"filled_qty": 10}


def op(name: str, effect: str, handler: str, required: list[str], **binding: Any) -> Any:
    return OperationDescriptor.model_validate(
        {
            "tool": {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "properties": {k: {} for k in (*required, "record_id", "qty")},
                    "required": required,
                },
                "outputSchema": {"type": "object"},
            },
            "cardea": {"version": 1, "effect": effect, "handler": handler, **binding},
        }
    )


def registry(package: Package) -> Registry:
    return Registry(
        (
            op("vcs.open_change", "effect", "pkg.open_change", ["record_id"]),
            op("vcs.read_change", "read", "pkg.read", []),
            op("vcs.comment", "effect", "pkg.read", []),
            op("ci.status", "read", "pkg.read", []),
            op("risk.check", "read", "pkg.read", []),
            op("market.quotes", "read", "pkg.read", []),
            op(
                "broker.place_order",
                "effect",
                "pkg.place",
                ["order_ref", "record_id", "qty"],
                idempotency_scope=["/order_ref"],
                reconciler="pkg.find",
            ),
            op("broker.fills", "read", "pkg.fills", ["order_ref", "record_id"]),
        ),
        {
            "pkg.open_change": package.open_change,
            "pkg.read": package.read,
            "pkg.place": package.place,
            "pkg.find": package.find,
            "pkg.fills": package.fills,
        },
    )


# Configuration only: the two examples' documents, as in ADR-034.

ENG_WORK: dict[str, Any] = {
    "supervisor": "sup-eng",
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "assignee_pool": {"type": "string"},
        },
        "required": ["title"],
    },
    "claims": {"lease_seconds": 14400, "max_per_holder": 1, "max_followups": 6},
    "states": ["open", "ready", "in_progress", "in_review", "done", "cancelled"],
    "initial": "open",
    "terminal": ["done", "cancelled"],
    "transitions": [
        {"name": "triage", "from": ["open"], "to": "ready",
         "guard": "args.assignee_pool == 'eng-team.dev'"},
        {"name": "start", "from": ["ready"], "to": "in_progress", "claim": "acquire",
         "on_claim_expiry": "ready", "guard": "caller.pool == record.data.assignee_pool"},
        {"name": "submit", "from": ["in_progress"], "to": "in_review", "claim": "holder",
         "requires_receipt": {"operation": "vcs.open_change", "in": "same_task"}},
        {"name": "request_changes", "from": ["in_review"], "to": "in_progress",
         "guard": "caller.id != record.holder"},
        {"name": "approve", "from": ["in_review"], "to": "done", "claim": "release",
         "guard": "caller.id != record.holder"},
        {"name": "cancel", "from": ["open", "ready", "in_progress", "in_review"],
         "to": "cancelled", "claim": "release"},
    ],
}
ENG_DECISIONS: dict[str, Any] = {
    "supervisor": "sup-eng",
    "schema": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["title", "summary"],
    },
}
ENG_POOLS: dict[str, tuple[int, tuple[str, ...]]] = {
    "principal": (1, ("record:work/read", "record:work/approve", "record:work/request_changes",
                      "record:decisions/create", "record:decisions/read", "vcs.read_change",
                      "vcs.comment")),
    "product": (1, ("record:work/create", "record:work/read", "ci.status")),
    "lead": (1, ("record:work/read", "record:work/update", "record:work/triage",
                 "record:work/cancel")),
    "dev": (3, ("record:work/read", "record:work/start", "record:work/submit",
                "vcs.open_change", "ci.status")),
}

DESK_ORDERS: dict[str, Any] = {
    "supervisor": "sup-desk",
    "schema": {
        "type": "object",
        "required": ["instrument", "side", "qty", "limit_price", "rationale"],
        "properties": {
            "instrument": {"type": "string"},
            "side": {"enum": ["buy", "sell"]},
            "qty": {"type": "number", "exclusiveMinimum": 0},
            "limit_price": {"type": "number"},
            "rationale": {"type": "string"},
            "broker_order_id": {"type": "string"},
            "filled_qty": {"type": "number"},
        },
    },
    "claims": {"lease_seconds": 3600, "max_per_holder": 4, "max_followups": 3},
    "states": ["proposed", "approved", "placed", "filled", "reconciled", "rejected", "expired",
               "cancelled"],
    "initial": "proposed",
    "terminal": ["reconciled", "rejected", "expired", "cancelled"],
    "transitions": [
        {"name": "approve", "from": ["proposed"], "to": "approved",
         "guard": "record.created_by != caller.id",
         "approval": {"level": "conditional", "when": "true",
                      "reason": "Every order needs a desk approver",
                      "expires_after_seconds": 3600, "on_expiry": "resume",
                      "distinct_from_supervisor": False}},
        {"name": "reject", "from": ["proposed"], "to": "rejected"},
        {"name": "place", "from": ["approved"], "to": "placed", "claim": "acquire",
         "on_claim_expiry": "approved",
         "requires_receipt": {"operation": "broker.place_order", "in": "same_task"}},
        {"name": "fill", "from": ["placed"], "to": "filled", "claim": "holder"},
        {"name": "reconcile", "from": ["filled"], "to": "reconciled", "claim": "release",
         "requires_receipt": {"operation": "broker.fills", "in": "same_task"}},
        {"name": "expire", "from": ["proposed", "approved"], "to": "expired"},
    ],
}
DESK_POOLS: dict[str, tuple[int, tuple[str, ...]]] = {
    "analyst": (2, ("record:orders/create", "record:orders/read", "risk.check",
                    "market.quotes")),
    "risk": (1, ("record:orders/read", "record:orders/approve", "record:orders/reject",
                 "risk.check")),
    "execution": (1, ("record:orders/read", "record:orders/place", "record:orders/fill",
                      "record:orders/reconcile", "broker.place_order", "broker.fills")),
}


def manifest(group: str, namespace: str, supervisor: str, collections: list[str],
             pools: dict[str, tuple[int, tuple[str, ...]]]) -> AgentGroup:
    return AgentGroup.model_validate(
        {
            "apiVersion": "cardea/v1alpha1",
            "kind": "AgentGroup",
            "metadata": {"name": group, "namespace": namespace},
            "spec": {
                "supervisor": supervisor,
                "collections": collections,
                "pools": [
                    {
                        "name": name,
                        "replicas": replicas,
                        "grants": [*RUN, *grants],
                        "run": {
                            "instructions": f"Do the {name} work.",
                            "operations": ["records.get"],
                            "model": "model-a",
                            "bounds": {"timeout_seconds": 60, "attempts": 2},
                        },
                        "triggers": [{"schedule": {"cron": "0 9 29 2 *", "timezone": "UTC",
                                                   "misfire": "skip"}}],
                    }
                    for name, (replicas, grants) in pools.items()
                ],
            },
        }
    )


def provisioning() -> Provisioning:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    principals = [
        {"principal_id": p, "kind": "service" if p == "runtime" else "human",
         "auth_kind": "spiffe", "issuer": "test", "subject": p}
        for p in sorted(TOKENS)
    ] + [{"principal_id": "risk-desk", "kind": "agent", "runtime_id": "runtime"}]
    permissions: list[dict[str, Any]] = []
    platform: list[dict[str, Any]] = []
    for supervisor, namespace, pools in (
        ("sup-eng", "eng", ENG_POOLS),
        ("sup-desk", "desk", DESK_POOLS),
    ):
        rights = {*RIGHTS, *RUN, "groups.manage", *(g for _, gs in pools.values() for g in gs)}
        for right in sorted(rights):
            grant = {"namespace": namespace, "operation": right, "expires_at": expires}
            approve = right == "record:orders/approve"
            permissions.append({"principal_id": supervisor, "delegate": True, **grant})
            platform.append({"principal_id": "platform", "approve": approve, **grant})
    # A human desk approver may decide the `approve` transition of `orders` and nothing else.
    permissions.append(
        {"principal_id": "desk-approver", "namespace": "desk", "allow": False, "approve": True,
         "operation": "record:orders/approve", "expires_at": expires}
    )
    return Provisioning.model_validate(
        {"principals": principals, "permissions": permissions, "platform": platform}
    )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Kernel, Any, Package]]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    package = Package()
    installed = registry(package)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, provisioning(), installed)
    session = runs.FakeSession()
    entry = implicit_entry("model-a").model_copy(update={"models": {"model-a": ModelEntry()}})
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    kernel = Kernel(db, cast(Authentication, Auth()), operations=installed, harnesses=harnesses)
    try:
        yield kernel, session, package
    finally:
        db.close()


def configure(kernel: Kernel, namespace: str, supervisor: str, collections: dict[str, Any],
              group: AgentGroup) -> None:
    for name, spec in collections.items():
        document = CollectionDocument.model_validate(
            {"metadata": {"name": name, "namespace": namespace}, "spec": spec}
        )
        with kernel.database.engine.begin() as conn:
            apply_collection(conn, supervisor, document, dry_run=False)
    result = apply_group(
        kernel, kernel.auth.validate(supervisor), group.metadata.name, group, False
    )
    assert result["change"] == "created"


Script = Callable[[ClientSession], Awaitable[Any]]
STEPS = iter(range(10**6))


def act(
    kernel: Kernel,
    session: Any,
    *,
    namespace: str,
    supervisor: str,
    member: str,
    grants: tuple[str, ...],
    operations: list[str],
    script: Script,
    attempts: int = 1,
) -> tuple[str, Any]:
    """One run of a group member: the scripted agent calls its tools over MCP."""
    results: list[Any] = []

    async def play(client: ClientSession, request: SessionRequest) -> SessionResult:
        results.append(await script(client))
        return SessionResult(text="Done.", is_error=False, cost_usd=0.0, turns=1)

    session.scripts = [play] * attempts
    request = AgentRunRequest.model_validate(
        {
            "request_id": f"{member}.step-{next(STEPS)}",
            "agent_id": member,
            "namespace": namespace,
            "objective": "One step of the worked example",
            "instructions": "Do your step.",
            "operations": operations,
            "grants": [*RUN, *grants],
            "model": "model-a",
            "max_attempts": attempts,
            "attempt_timeout_seconds": 60,
        }
    )
    task_id = kernel.submit(kernel.auth.validate(supervisor), request).task.task_id
    state = AgentRunRuntime(kernel).execute(task_id)
    return state, results[-1] if results else None


async def tool(client: ClientSession, name: str, **arguments: Any) -> Any:
    result = await runs.call(client, name, **arguments)
    if result.is_error:
        return {"error": str(result.content[0].text)}  # type: ignore[union-attr]
    return result.structured_content


def state_of(kernel: Kernel, record_id: str) -> tuple[str, str | None, int]:
    with kernel.database.read() as conn:
        row = conn.exec_driver_sql(
            "SELECT state,holder,claim_epoch FROM records WHERE record_id=?", (record_id,)
        ).one()
    return str(row[0]), row[1], int(row[2])


# A. Software team: a review gate built from transitions, grants and guards


def test_software_team_review_gate(setup: tuple[Kernel, Any, Package]) -> None:
    kernel, session, _ = setup
    group = manifest("eng-team", "eng", "sup-eng", ["work", "decisions"], ENG_POOLS)
    configure(kernel, "eng", "sup-eng", {"work": ENG_WORK, "decisions": ENG_DECISIONS}, group)

    def step(member: str, operations: list[str], script: Script) -> Any:
        pool = member.split(".")[1]
        state, result = act(
            kernel,
            session,
            namespace="eng",
            supervisor="sup-eng",
            member=member,
            grants=ENG_POOLS[pool][1],
            operations=operations,
            script=script,
        )
        assert state == "succeeded", (member, result)
        return result

    def transition(record_id: str, name: str, **extra: Any) -> Script:
        async def script(client: ClientSession) -> Any:
            return await tool(
                client,
                "records-transition",
                collection="work",
                record_id=record_id,
                transition=name,
                **extra,
            )

        return script

    # Product files work.
    async def file(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-create",
            collection="work",
            client_key="login-flake",
            data={"title": "Flaky login test"},
        )

    record = step("eng-team.product.0", ["records.create"], file)["record"]
    rid = record["record_id"]
    assert record["state"] == "open"

    # The lead assigns it to the developers: data, then the triage guard.
    async def assign(client: ClientSession) -> Any:
        await tool(
            client,
            "records-update",
            collection="work",
            record_id=rid,
            expected_version=1,
            data={"title": "Flaky login test", "assignee_pool": "eng-team.dev"},
        )
        wrong = await tool(
            client,
            "records-transition",
            collection="work",
            record_id=rid,
            transition="triage",
            args={"assignee_pool": "eng-team.product"},
        )
        assert "guard_refused" in wrong["error"]
        return await tool(
            client,
            "records-transition",
            collection="work",
            record_id=rid,
            transition="triage",
            args={"assignee_pool": "eng-team.dev"},
        )

    assert step("eng-team.lead.0", ["records.update", "records.transition"], assign)[
        "record"
    ]["state"] == "ready"

    # Only the dev pool may start it, and only one developer holds it.
    refused = step("eng-team.lead.0", ["records.transition"], transition(rid, "start"))
    assert "task_grant_denied" in refused["error"]
    started = step("eng-team.dev.0", ["records.transition"], transition(rid, "start"))
    epoch = started["claim"]["claim_epoch"]
    assert state_of(kernel, rid) == ("in_progress", "eng-team.dev.0", epoch)

    # Submitting needs a change opened in the same run.
    def submit(member: str) -> Script:
        async def script(client: ClientSession) -> Any:
            early = await tool(
                client,
                "records-transition",
                collection="work",
                record_id=rid,
                transition="submit",
                claim_epoch=epoch,
            )
            assert "receipt_required" in early["error"], early
            await tool(client, "vcs-open_change", record_id=rid)
            return await tool(
                client,
                "records-transition",
                collection="work",
                record_id=rid,
                transition="submit",
                claim_epoch=epoch,
            )

        return script

    tools = ["records.transition", "vcs.open_change"]
    assert step("eng-team.dev.0", tools, submit("eng-team.dev.0"))["record"]["state"] == (
        "in_review"
    )
    # The holder cannot approve its own work; the principal requests changes.
    assert "task_grant_denied" in step(
        "eng-team.dev.0", ["records.transition"], transition(rid, "approve")
    )["error"]
    back = step("eng-team.principal.0", ["records.transition"], transition(rid, "request_changes"))
    assert back["record"]["state"] == "in_progress"
    assert back["record"]["holder"] == "eng-team.dev.0"  # the developer keeps the work
    step("eng-team.dev.0", tools, submit("eng-team.dev.0"))
    done = step("eng-team.principal.0", ["records.transition"], transition(rid, "approve"))
    assert done["record"]["state"] == "done" and done["record"]["holder"] is None

    # The decisions log is just another collection.
    async def decide(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-create",
            collection="decisions",
            client_key="login-retry",
            data={"title": "Retry login once", "summary": "Flaky upstream; retry once."},
        )

    assert step("eng-team.principal.0", ["records.create"], decide)["record"]["version"] == 1
    with kernel.database.read() as conn:
        path = [
            json_detail["to"]
            for (json_detail,) in (
                (__import__("json").loads(r[0]),)
                for r in conn.exec_driver_sql(
                    "SELECT detail_json FROM record_events WHERE record_id=? "
                    "AND kind='transitioned' ORDER BY event_id",
                    (rid,),
                )
            )
        ]
    assert path == ["ready", "in_progress", "in_review", "in_progress", "in_review", "done"]
