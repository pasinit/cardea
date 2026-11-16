"""ADR-034's two worked examples as executable tests (acceptance test 2; test 1 in part).

Both use only the kernel's generic primitives (collections, records, state machines, claims,
agent groups, approvals, mailboxes, event triggers), the configuration in `examples/teams`
and the fake reference operations package `packages/cardea-reference-ops`. Nothing in this
module or the kernel changes between the software team and the trading desk; only
configuration does.

The tests start each member's run directly rather than through its manifest triggers, so
each step is deterministic. The desk's gated `approve` transition waits inside the risk
agent's run for the human approver (ADR-036 slice 4) and runs as `apr-01` in its
continuation attempt; a broker fill arrives as a signed ADR-035 webhook.
"""

import base64
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from math import floor
from pathlib import Path
from typing import Any, cast

import pytest
import test_agent_runs as runs
from cardea_reference_ops import Fakes, descriptors
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from standardwebhooks.webhooks import Webhook

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, approval_requests, events, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault
from cardea.service.groups import apply as apply_group
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    Provisioning,
    TriggerSpec,
    now,
)
from cardea.service.operations import (
    Registry,
)
from cardea.service.records import CollectionDocument, apply_collection

TOKENS = {"runtime", "sup-eng", "sup-desk", "desk-approver"}
# A Standard Webhooks test secret for the fake broker (not a real credential).
SECRET = "whsec_" + base64.b64encode(b"broker-fills-example-key-32bytes").decode()
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


# The operations package (`packages/cardea-reference-ops`) and the configuration
# (`examples/teams`): nothing below is specific to either example.

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "teams"


def example(name: str, document: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((EXAMPLES / name / document).read_text()))


def registry(package: Fakes) -> Registry:
    return Registry(descriptors(), package.handlers())


def example_group(name: str) -> AgentGroup:
    return AgentGroup.model_validate(example(name, "group.json"))


def example_collections(name: str) -> dict[str, Any]:
    return {
        path.stem.removeprefix("collection."): json.loads(path.read_text())["spec"]
        for path in sorted((EXAMPLES / name).glob("collection.*.json"))
    }


def pools(name: str) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Each pool's replicas and its grants beyond the run basics, from the manifest."""
    return {
        pool.name: (pool.replicas, tuple(g for g in pool.grants if g not in RUN))
        for pool in example_group(name).spec.pools
    }


ENG_POOLS = pools("software-team")
DESK_POOLS = pools("trading-desk")


def provisioning() -> Provisioning:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    principals = [
        {
            "principal_id": p,
            "kind": "service" if p == "runtime" else "human",
            "auth_kind": "spiffe",
            "issuer": "test",
            "subject": p,
        }
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
        {
            "principal_id": "desk-approver",
            "namespace": "desk",
            "allow": False,
            "approve": True,
            "operation": "record:orders/approve",
            "expires_at": expires,
        }
    )
    return Provisioning.model_validate(
        {"principals": principals, "permissions": permissions, "platform": platform}
    )


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Kernel, Any, Fakes]]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    package = Fakes()
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


def configure(
    kernel: Kernel, namespace: str, supervisor: str, collections: dict[str, Any], group: AgentGroup
) -> None:
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


# ADR-036 slice 4: a gated transition held in an agent run keeps its resource-qualified
# approval check when the continuation re-executes it


def test_held_transition_reexecution_keeps_the_resource_qualified_check(
    setup: tuple[Kernel, Any, Fakes],
) -> None:
    kernel, session, _ = setup
    group = example_group("trading-desk")
    configure(kernel, "desk", "sup-desk", example_collections("trading-desk"), group)

    async def propose(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-create",
            collection="orders",
            client_key="o-held",
            data={
                "instrument": "XYZ",
                "side": "buy",
                "qty": 1,
                "limit_price": 10.5,
                "rationale": "Test",
            },
        )

    made = act(
        kernel,
        session,
        namespace="desk",
        supervisor="sup-desk",
        member="desk.analyst.0",
        grants=DESK_POOLS["analyst"][1],
        operations=["records.create", "risk.check"],
        script=propose,
    )
    order = str(made[1]["record"]["record_id"])

    async def approve(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-transition",
            collection="orders",
            record_id=order,
            transition="approve",
        )

    held_state, held = act(
        kernel,
        session,
        namespace="desk",
        supervisor="sup-desk",
        member="desk.risk.0",
        grants=DESK_POOLS["risk"][1],
        operations=["records.transition"],
        script=approve,
    )
    assert held_state == "waiting" and "approval_pending" in held["error"]
    with kernel.database.read() as conn:
        request_id, digest, task_id = conn.exec_driver_sql(
            "SELECT request_id,intent_digest,task_id FROM approval_requests "
            "WHERE approval_resource='record:orders/approve'"
        ).one()
    client = TestClient(create_app(kernel))
    decided = client.post(
        f"/v0.2/approvals/{request_id}/decision",
        json={"decision_id": "d-held", "decision": "approve", "intent_digest": digest},
        headers={"Authorization": "Bearer desk-approver", "X-Cardea-Auth": "spiffe"},
    )
    assert decided.status_code == 200, decided.json()
    # Before the continuation runs, the approver keeps only an operation-level `approve`
    # row on `records.transition`, which does not satisfy a resource-qualified request.
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE policies SET operation='records.transition' "
            "WHERE principal_id='desk-approver' AND operation='record:orders/approve'"
        )
    prompts: list[str] = []

    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        prompts.append(request.prompt)
        return SessionResult(text="The approval no longer holds.", is_error=False, turns=1)

    session.scripts = [resumed]
    assert AgentRunRuntime(kernel).execute(str(task_id)) == "succeeded"
    assert state_of(kernel, order)[0] == "proposed"  # the transition never ran
    with kernel.database.read() as conn:
        state, reason = conn.exec_driver_sql(
            "SELECT state,void_reason FROM approval_requests WHERE request_id=?", (request_id,)
        ).one()
        receipts = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM operations WHERE task_id=? AND operation_id='apr-01'",
            (task_id,),
        ).scalar_one()
    assert (state, reason) == ("void", "approver_authority_lost") and receipts == 0
    assert 'decision="void"' in prompts[0] and "approver_authority_lost" in prompts[0]


# A. Software team: a review gate built from transitions, grants and guards


def test_software_team_review_gate(setup: tuple[Kernel, Any, Fakes]) -> None:
    kernel, session, _ = setup
    group = example_group("software-team")
    configure(kernel, "eng", "sup-eng", example_collections("software-team"), group)

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

    assert (
        step("eng-team.lead.0", ["records.update", "records.transition"], assign)["record"]["state"]
        == "ready"
    )

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
    assert (
        "task_grant_denied"
        in step("eng-team.dev.0", ["records.transition"], transition(rid, "approve"))["error"]
    )
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
            json.loads(r[0])["to"]
            for r in conn.exec_driver_sql(
                "SELECT detail_json FROM record_events WHERE record_id=? "
                "AND kind='transitioned' ORDER BY event_id",
                (rid,),
            )
        ]
    assert path == ["ready", "in_progress", "in_review", "in_progress", "in_review", "done"]


# B. Trading desk: propose, approve (human), place, fill, reconcile


def fill_trigger(secret: Path) -> TriggerSpec:
    """The broker's fill webhook (ADR-035), host-owned, waking the execution member."""
    document = example("trading-desk", "trigger.fills.json")
    document["event"]["secret_file"] = str(secret)
    return TriggerSpec.model_validate(document)


def deliver_fill(kernel: Kernel, secret: str, order: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "specversion": "1.0",
            "id": f"fill-{order}",
            "source": "broker",
            "type": "fill",
            "subject": order,
            "datacontenttype": "application/json",
            "data": {"order_ref": order, "filled_qty": 10},
        }
    ).encode()
    stamp = floor(time.time())
    signature = Webhook(secret).sign(
        f"msg-{order}", datetime.fromtimestamp(stamp, tz=UTC), body.decode()
    )
    headers = {
        "webhook-id": f"msg-{order}",
        "webhook-timestamp": str(stamp),
        "webhook-signature": signature,
        "content-type": "application/cloudevents+json",
    }
    return events.deliver(kernel, "desk-fills", headers, body)


def test_trading_desk_order_flow(setup: tuple[Kernel, Any, Fakes], tmp_path: Path) -> None:
    kernel, session, package = setup
    group = example_group("trading-desk")
    configure(kernel, "desk", "sup-desk", example_collections("trading-desk"), group)

    def step(member: str, operations: list[str], script: Script, attempts: int = 1) -> Any:
        pool = member.split(".")[1]
        state, result = act(
            kernel,
            session,
            namespace="desk",
            supervisor="sup-desk",
            member=member,
            grants=DESK_POOLS[pool][1],
            operations=operations,
            script=script,
            attempts=attempts,
        )
        assert state == "succeeded", (member, result)
        return result

    def transition(record_id: str, name: str, **extra: Any) -> Script:
        async def script(client: ClientSession) -> Any:
            return await tool(
                client,
                "records-transition",
                collection="orders",
                record_id=record_id,
                transition=name,
                **extra,
            )

        return script

    def propose(key: str, qty: float) -> str:
        async def script(client: ClientSession) -> Any:
            await tool(client, "risk-check")
            return await tool(
                client,
                "records-create",
                collection="orders",
                client_key=key,
                data={
                    "instrument": "XYZ",
                    "side": "buy",
                    "qty": qty,
                    "limit_price": 10.5,
                    "rationale": "Mean reversion",
                },
            )

        made = step("desk.analyst.0", ["records.create", "risk.check"], script)
        return str(made["record"]["record_id"])

    # 1. An analyst proposes; the risk agent rejects one proposal outright.
    rejected = propose("o-reject", 5)
    out = step("desk.risk.0", ["records.transition"], transition(rejected, "reject"))
    assert out["record"]["state"] == "rejected"
    order = propose("o-1", 10)

    # 2. Approval needs a human desk approver. Inside the risk agent's run the gated call
    # is held (ADR-036 slice 4): the run waits and nothing runs yet.
    held_state, held = act(
        kernel,
        session,
        namespace="desk",
        supervisor="sup-desk",
        member="desk.risk.0",
        grants=DESK_POOLS["risk"][1],
        operations=["records.transition"],
        script=transition(order, "approve"),
    )
    assert held_state == "waiting" and "approval_pending" in held["error"], held
    assert state_of(kernel, order)[0] == "proposed"
    with kernel.database.read() as conn:
        pending = conn.exec_driver_sql(
            "SELECT request_id,intent_digest,approval_resource,task_id,operation_id "
            "FROM approval_requests ORDER BY created_at DESC LIMIT 1"
        ).one()
    assert pending[0] in held["error"]
    assert pending[2] == "record:orders/approve" and pending[4] == "apr-01"
    decided = TestClient(create_app(kernel)).post(
        f"/v0.2/approvals/{pending[0]}/decision",
        json={"decision_id": "d-1", "decision": "approve", "intent_digest": pending[1]},
        headers={"Authorization": "Bearer desk-approver", "X-Cardea-Auth": "spiffe"},
    )
    assert decided.status_code == 200, decided.json()

    # The continuation attempt runs the approved transition as apr-01, then resumes.
    async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert 'decision="approved"' in request.prompt
        return SessionResult(text="Approved and recorded.", is_error=False, turns=1)

    session.scripts = [resumed]
    assert AgentRunRuntime(kernel).execute(str(pending[3])) == "succeeded"
    assert state_of(kernel, order)[0] == "approved"

    # 3. Execution places it. The first attempt crashes after placing; the retry's
    # placement is deduplicated by its effect scope and the order is placed once.
    attempt = iter(range(3))

    async def place(client: ClientSession) -> Any:
        placed = await tool(client, "broker-place_order", order_ref=order, record_id=order, qty=10)
        if next(attempt) == 0:
            raise ConnectionError("harness died after placing")
        assert placed["broker_order_id"].startswith("B-")
        return await tool(
            client,
            "records-transition",
            collection="orders",
            record_id=order,
            transition="place",
        )

    out = step("desk.execution.0", ["broker.place_order", "records.transition"], place, 2)
    assert package.placements == 1
    epoch = out["claim"]["claim_epoch"]
    assert state_of(kernel, order) == ("placed", "desk.execution.0", epoch)

    # 4. A fill arrives as a signed ADR-035 webhook and wakes the execution member, which
    # records it. A redelivered webhook starts nothing more.
    secret_file = tmp_path / "broker.secret"
    secret_file.write_text(SECRET + "\n")
    secret_file.chmod(0o600)
    provision(
        kernel.database,
        provisioning().model_copy(update={"triggers": (fill_trigger(secret_file),)}),
        kernel.operations,
    )
    woken = deliver_fill(kernel, SECRET, order)
    assert woken["outcome"] == "admitted"
    assert deliver_fill(kernel, SECRET, order)["outcome"] == "duplicate"
    recorded: list[Any] = []

    async def on_fill(client: ClientSession, request: SessionRequest) -> SessionResult:
        assert '"type": "fill"' in request.prompt and order in request.prompt
        recorded.append(await transition(order, "fill", claim_epoch=epoch)(client))
        return SessionResult(text="Fill recorded.", is_error=False, turns=1)

    session.scripts = [on_fill]
    assert AgentRunRuntime(kernel).execute(str(woken["task_id"])) == "succeeded"
    assert recorded[0]["record"]["state"] == "filled"

    # 5. Reconciliation needs the broker's fills read in the same run, and releases.
    async def reconcile(client: ClientSession) -> Any:
        await tool(client, "broker-fills", order_ref=order, record_id=order)
        return await tool(
            client,
            "records-transition",
            collection="orders",
            record_id=order,
            transition="reconcile",
            claim_epoch=epoch,
        )

    done = step("desk.execution.0", ["broker.fills", "records.transition"], reconcile)
    assert done["record"]["state"] == "reconciled" and done["record"]["holder"] is None

    # A risk-limit breach is refused by the broker operation itself.
    big = propose("o-big", 5000)

    async def too_big(client: ClientSession) -> Any:
        return await tool(client, "broker-place_order", order_ref=big, record_id=big, qty=5000)

    refused = step("desk.execution.0", ["broker.place_order"], too_big)
    assert "risk_limit" in refused["error"]


def test_trading_desk_gate_rejects_and_expires(setup: tuple[Kernel, Any, Fakes]) -> None:
    """The gated `approve` transition: a human rejection and an expiry both leave the
    order unapproved; after the expiry the risk agent moves it to `expired`."""
    kernel, session, _ = setup
    group = example_group("trading-desk")
    configure(kernel, "desk", "sup-desk", example_collections("trading-desk"), group)

    async def propose_script(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-create",
            collection="orders",
            client_key=f"o-{next(STEPS)}",
            data={
                "instrument": "XYZ",
                "side": "sell",
                "qty": 3,
                "limit_price": 9.5,
                "rationale": "Trim",
            },
        )

    def propose() -> str:
        state, made = act(
            kernel,
            session,
            namespace="desk",
            supervisor="sup-desk",
            member="desk.analyst.0",
            grants=DESK_POOLS["analyst"][1],
            operations=["records.create"],
            script=propose_script,
        )
        assert state == "succeeded", made
        return str(made["record"]["record_id"])

    def held(order: str) -> tuple[str, str, str]:
        async def approve(client: ClientSession) -> Any:
            return await tool(
                client,
                "records-transition",
                collection="orders",
                record_id=order,
                transition="approve",
            )

        state, result = act(
            kernel,
            session,
            namespace="desk",
            supervisor="sup-desk",
            member="desk.risk.0",
            grants=DESK_POOLS["risk"][1],
            operations=["records.transition"],
            script=approve,
        )
        assert state == "waiting" and "approval_pending" in result["error"], result
        with kernel.database.read() as conn:
            row = conn.exec_driver_sql(
                "SELECT request_id,intent_digest,task_id FROM approval_requests "
                "ORDER BY created_at DESC LIMIT 1"
            ).one()
        return str(row[0]), str(row[1]), str(row[2])

    def resume(task_id: str, decision: str, then: Script | None = None) -> Any:
        results: list[Any] = []

        async def resumed(client: ClientSession, request: SessionRequest) -> SessionResult:
            assert f'decision="{decision}"' in request.prompt
            if then is not None:
                results.append(await then(client))
            return SessionResult(text=f"The approval was {decision}.", is_error=False, turns=1)

        session.scripts = [resumed]
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
        return results[0] if results else None

    # A human approver rejects the held transition: it never runs.
    rejected = propose()
    request_id, digest, task_id = held(rejected)
    decided = TestClient(create_app(kernel)).post(
        f"/v0.2/approvals/{request_id}/decision",
        json={"decision_id": "d-reject", "decision": "reject", "intent_digest": digest},
        headers={"Authorization": "Bearer desk-approver", "X-Cardea-Auth": "spiffe"},
    )
    assert decided.status_code == 200, decided.json()
    resume(task_id, "rejected")
    assert state_of(kernel, rejected)[0] == "proposed"

    # Nobody decides within an hour: the request expires and the run resumes (on_expiry:
    # resume) without the transition; the risk agent then expires the order itself.
    stale = propose()
    request_id, _, task_id = held(stale)
    # The hour passes: backdate the deadline and tick at real time, as the ADR-036 expiry
    # tests do; a future clock would stamp the woken task ahead of later transitions.
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS approval_intent_immutable")
        conn.exec_driver_sql(
            "UPDATE approval_requests SET expires_at=? WHERE request_id=?",
            ((now() - timedelta(seconds=1)).isoformat(), request_id),
        )
    assert approval_requests.expire_due(kernel) == {request_id: "expired"}

    async def expire(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-transition",
            collection="orders",
            record_id=stale,
            transition="expire",
        )

    expired = resume(task_id, "expired", expire)
    assert expired["record"]["state"] == "expired", expired


def test_the_reference_package_installs_like_any_operations_package(tmp_path: Path) -> None:
    """Descriptors in a private `operations_dir`, handlers from installed entry points."""
    directory = tmp_path / "operations"
    directory.mkdir(mode=0o700)
    for descriptor in descriptors():
        path = directory / f"{descriptor.name}.json"
        path.write_text(descriptor.model_dump_json(by_alias=True))
        path.chmod(0o600)
    installed = Registry.load(directory)
    names = {d.name for d in descriptors()}
    assert names <= set(installed.descriptors)
    for name in names:
        assert installed.handler(name) is not None
        if installed.get(name).cardea.effect == "effect":
            # Every fake effect is scoped and can be reconciled from the fake store.
            assert installed.get(name).cardea.idempotency_scope
            assert installed.reconciler(name) is not None
