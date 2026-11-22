"""Record state machines (ADR-034 slice 3): the transition table, guards, claim effects,
receipt evidence, migration and approval gates; portable fakes only."""

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cardea_reference_ops import Fakes, descriptors
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError
from test_records import FakeClient, events, run

from cardea.service import kernel as handlers
from cardea.service import operation_runtime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
from cardea.service.kernel import Kernel, provision
from cardea.service.models import RIGHTS, OperationTaskRequest, Provisioning, now
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import (
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Registry,
)
from cardea.service.records import CollectionDocument, check_condition
from cardea.service.triggers import Scheduler

TOKENS = {"runtime", "supervisor", "approver", "operator", "outsider"}
HOUR = timedelta(hours=1)
WORK_ACTIONS = ("create", "read", "update", "triage", "start", "submit", "rework", "approve")
WORK = tuple(f"record:work/{a}" for a in (*WORK_ACTIONS, "broken"))
ORDERS = ("record:orders/create", "record:orders/read", "record:orders/approve")
SUPERVISOR = (
    *RIGHTS,
    "groups.manage",
    "vcs.open_change",
    "ci.report",
    "ci.run",
    *WORK,
    *ORDERS,
)


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in TOKENS:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


def document(supervisor: tuple[str, ...] = SUPERVISOR) -> Provisioning:
    expires = (now() + HOUR).isoformat()
    humans = ("runtime", "supervisor", "approver", "operator", "outsider")
    principals = [
        {
            "principal_id": p,
            "kind": "service" if p == "runtime" else "human",
            "auth_kind": "spiffe",
            "issuer": "test",
            "subject": p,
        }
        for p in humans
    ] + [{"principal_id": a, "kind": "agent", "runtime_id": "runtime"} for a in ("a1", "a2")]
    grant = {"namespace": "test", "expires_at": expires}
    permissions = [
        {"principal_id": "supervisor", "operation": r, "delegate": True, **grant}
        for r in supervisor
    ] + [
        # Approve-only on one transition of one collection (ADR-036 amendment).
        {
            "principal_id": "approver",
            "operation": "record:orders/approve",
            "allow": False,
            "approve": True,
            **grant,
        },
        # Operation-level approve on records.transition: must not satisfy a transition gate.
        {
            "principal_id": "operator",
            "operation": "records.transition",
            "allow": False,
            "approve": True,
            **grant,
        },
    ]
    platform = [
        {"principal_id": "platform", "operation": r, **grant}
        for r in supervisor
        if r != "record:orders/approve"
    ] + [
        {
            "principal_id": "platform",
            "operation": "record:orders/approve",
            "approve": True,
            **grant,
        },
        {
            "principal_id": "platform",
            "operation": "records.transition",
            "allow": False,
            "approve": True,
            **grant,
        },
    ]
    return Provisioning.model_validate(
        {"principals": principals, "permissions": permissions, "platform": platform}
    )


# The fake reference operations package provides `vcs.open_change` (no network).


async def report(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    return {"reported": True}


# A call that names its record only through its idempotency context `<record_id>.<epoch>`.
CI = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "ci.report",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.ci"},
    }
)


async def ci_run(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    """A stand-in for `env.try`: its output reports whether every scenario passed."""
    passed = bool(arguments.get("passed"))
    outcome = "pass" if passed else "fail"
    return {"passed": passed, "scenarios": [{"name": "try", "outcome": outcome}]}


CI_RUN = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "ci.run",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "read", "handler": "test.ci_run"},
    }
)
REGISTRY = Registry(
    (*descriptors(), CI, CI_RUN),
    {**Fakes().handlers(), "test.ci": report, "test.ci_run": ci_run},
)


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(), REGISTRY)
    instance = Kernel(db, cast(Authentication, Auth()), operations=REGISTRY)
    try:
        yield instance
    finally:
        db.close()


WORK_SPEC: dict[str, Any] = {
    "supervisor": "supervisor",
    "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
    "claims": {"lease_seconds": 3600},
    "states": ["open", "ready", "doing", "review", "done"],
    "initial": "open",
    "terminal": ["done"],
    "transitions": [
        {"name": "triage", "from": ["open"], "to": "ready", "guard": "args.priority > 0"},
        {"name": "broken", "from": ["open"], "to": "ready", "guard": "record.data.nope == 1"},
        {
            "name": "start",
            "from": ["ready"],
            "to": "doing",
            "claim": "acquire",
            "on_claim_expiry": "ready",
        },
        {
            "name": "submit",
            "from": ["doing"],
            "to": "review",
            "claim": "holder",
            "requires_receipt": {
                "operation": "vcs.open_change",
                "in": "same_caller",
                "window_seconds": 3600,
            },
        },
        {
            "name": "rework",
            "from": ["review"],
            "to": "doing",
            "guard": "caller.id != record.created_by",
        },
        {
            "name": "approve",
            "from": ["review"],
            "to": "done",
            "claim": "release",
            "guard": "caller.id != record.holder",
        },
    ],
}


def apply(kernel: Kernel, name: str, spec: dict[str, Any], dry_run: bool = False) -> Any:
    client = TestClient(create_app(kernel))
    body = {"metadata": {"name": name, "namespace": "test"}, "spec": spec}
    return client.put(
        f"/v0.2/collections/{name}",
        json={"apiVersion": "cardea/v1alpha1", "kind": "Collection", **body},
        params={"dry_run": str(dry_run).lower()},
        headers={"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"},
    )


@pytest.fixture
def work(kernel: Kernel) -> Kernel:
    response = apply(kernel, "work", WORK_SPEC)
    assert response.status_code == 200, response.json()
    return kernel


ALL = (*WORK, "vcs.open_change")


def create(kernel: Kernel, key: str = "k1") -> dict[str, Any]:
    state, output, code = run(
        kernel,
        "records.create",
        {"collection": "work", "client_key": key, "data": {"title": key}},
        grants=ALL,
    )
    assert state == "succeeded", code
    assert output is not None
    return cast(dict[str, Any], output["record"])


def move(
    kernel: Kernel,
    record: dict[str, Any],
    transition: str,
    agent: str = "a1",
    grants: tuple[str, ...] = ALL,
    **changes: Any,
) -> tuple[str, dict[str, Any] | None, str | None]:
    return run(
        kernel,
        "records.transition",
        {
            "collection": "work",
            "record_id": record["record_id"],
            "transition": transition,
            **changes,
        },
        agent=agent,
        grants=grants,
    )


def stored(kernel: Kernel, record_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        return dict(
            conn.exec_driver_sql("SELECT * FROM records WHERE record_id=?", (record_id,))
            .mappings()
            .one()
        )


def journal(kernel: Kernel, kind: str) -> list[dict[str, Any]]:
    with kernel.database.read() as conn:
        return [
            json.loads(r[0])
            for r in conn.exec_driver_sql(
                "SELECT payload FROM events WHERE event_type=? ORDER BY cursor",
                (f"cardea.record.{kind}",),
            )
        ]


# The table


@pytest.mark.parametrize(
    "change",
    [
        {"initial": "missing"},
        {"terminal": ["gone"]},
        {"transitions": [{"name": "x", "from": ["done"], "to": "open"}]},  # leaves terminal
        {"transitions": [{"name": "x", "from": ["open"], "to": "nowhere"}]},
        {"transitions": [{"name": "update", "from": ["open"], "to": "ready"}]},  # reserved
        {"transitions": [{"name": "x", "from": ["open"], "to": "ready"}] * 2},
        {
            "transitions": [
                {"name": "x", "from": ["open"], "to": "ready", "on_claim_expiry": "open"}
            ]
        },  # needs acquire
        {"transitions": [{"name": "x", "from": ["open"], "to": "ready", "guard": "ns == 1"}]},
        {
            "transitions": [
                {
                    "name": "x",
                    "from": ["open"],
                    "to": "ready",
                    "approval": {"level": "conditional", "when": "a" * 520, "reason": "too long"},
                }
            ]
        },
        {"claims": None},  # claim effects need claims
        {"states": None},
        {"migrate": [{"from": "open", "to": "ready"}]},  # open is still declared
    ],
)
def test_invalid_tables_are_refused(change: dict[str, Any]) -> None:
    spec = {**WORK_SPEC, **change}
    if spec.get("claims") is None:
        spec.pop("claims")
    with pytest.raises(ValidationError):
        CollectionDocument.model_validate(
            {"metadata": {"name": "work", "namespace": "test"}, "spec": spec}
        )


def test_records_start_in_the_initial_state(work: Kernel) -> None:
    record = create(work)
    assert record["state"] == "open"
    _, found, _ = run(work, "records.query", {"collection": "work", "state": "open"}, grants=ALL)
    assert found is not None and [r["record_id"] for r in found["records"]] == [record["record_id"]]


# Permissions, from states and guards


def test_each_transition_needs_its_own_grant_and_a_from_state(work: Kernel) -> None:
    record = create(work)
    no_grant = tuple(g for g in ALL if g != "record:work/triage")
    assert move(work, record, "triage", grants=no_grant, args={"priority": 1})[2] == (
        "task_grant_denied"
    )
    assert move(work, record, "start")[2] == "transition_not_allowed"
    provision(work.database, document((*SUPERVISOR, "record:work/other")), REGISTRY)
    assert move(work, record, "other", grants=(*ALL, "record:work/other"))[2] == (
        "transition_unknown"
    )
    _, output, _ = move(work, record, "triage", args={"priority": 2})
    assert output is not None and output["record"]["state"] == "ready"
    assert output["record"]["version"] == 2


def test_guards_refuse_and_fail_closed(work: Kernel) -> None:
    record = create(work)
    assert move(work, record, "triage", args={"priority": 0})[2] == "guard_refused"
    assert move(work, record, "triage")[2] == "guard_refused"  # args.priority is missing
    assert move(work, record, "broken")[2] == "guard_refused"  # evaluation error
    assert stored(work, record["record_id"])["state"] == "open"


def refusal(kernel: Kernel, record: dict[str, Any], transition: str, **changes: Any) -> str:
    """The message of the last call's failure, after checking that it failed."""
    state, _, code = move(kernel, record, transition, **changes)
    assert state != "succeeded" and code is not None
    with kernel.database.read() as conn:
        snapshot = conn.exec_driver_sql(
            "SELECT snapshot FROM operations ORDER BY rowid DESC LIMIT 1"
        ).scalar_one()
    failure = json.loads(snapshot)["failure"]
    assert failure["code"] == code
    # What the agent is told: the bounded summary of the handler's own message.
    return f"{code}: {failure['details']['summary']}"


def test_an_unknown_transition_lists_those_from_the_records_state(work: Kernel) -> None:
    record = create(work)
    assert refusal(work, record, "nope") == (
        'transition_unknown: Collection work has no transition "nope"; from state "open" use '
        '"triage" or "broken"'
    )


def test_a_state_name_points_to_the_transition_into_it(work: Kernel) -> None:
    record = create(work)
    assert refusal(work, record, "review") == (
        'transition_unknown: "review" is a state, not a transition; the transition into it is '
        '"submit"; from state "open" use "triage" or "broken"'
    )
    assert '"ready" is a state, not a transition; the transition into it is "triage" or ' in (
        refusal(work, record, "ready")
    )
    # A name the caller is granted but the table lacks gets the same help.
    provision(work.database, document((*SUPERVISOR, "record:work/open")), REGISTRY)
    assert '"open" is a state, not a transition; none leads into it' in (
        refusal(work, record, "open", grants=(*ALL, "record:work/open"))
    )
    assert stored(work, record["record_id"])["state"] == "open"


def test_a_caller_without_a_grant_on_the_collection_learns_nothing_of_its_table(
    work: Kernel,
) -> None:
    record = create(work)
    message = refusal(work, record, "ready", grants=("record:orders/read",))
    assert message == (
        "task_grant_denied: Operation is outside explicit task grants: record:work/ready is "
        "not held by the task; caller a1"
    )
    # A transition that exists but is not granted is still a permission refusal.
    no_grant = tuple(g for g in ALL if g != "record:work/triage")
    assert refusal(work, record, "triage", grants=no_grant, args={"priority": 1}) == (
        "task_grant_denied: Operation is outside explicit task grants: record:work/triage is "
        "not held by the task; caller a1"
    )


def test_a_guard_refusal_names_the_guard_and_the_caller(work: Kernel) -> None:
    record = create(work)
    assert refusal(work, record, "triage", args={"priority": 0}) == (
        "guard_refused: Transition triage guard refused this call for a1: args.priority > 0"
    )
    caller: dict[str, JsonValue] = {"id": "a1", "group": "team", "pool": "team.builders"}
    with pytest.raises(OperationRejected, match=r"for a1 \(pool team\.builders\): args\.x"):
        check_condition("args.x == 1", "Transition triage", {}, {"x": 2}, caller)


def test_transitions_are_history_and_cloudevents(work: Kernel) -> None:
    record = create(work)
    move(work, record, "triage", args={"priority": 1})
    with work.database.read() as conn:
        detail = conn.exec_driver_sql(
            "SELECT kind,detail_json FROM record_events WHERE record_id=? AND kind='transitioned'",
            (record["record_id"],),
        ).one()
    assert json.loads(detail[1]) == {
        "transition": "triage",
        "from": "open",
        "to": "ready",
        "claim": "none",
        "args": {"priority": 1},
        "receipt_id": None,
    }
    (event,) = journal(work, "transitioned")
    assert event["type"] == "cardea.record.transitioned" and event["specversion"] == "1.0"
    assert event["subject"] == f"work/{record['record_id']}"
    assert event["source"] == "cardea://test"
    data = event["data"]
    assert (data["from"], data["to"], data["transition"]) == ("open", "ready", "triage")


# Claim effects


def ready(kernel: Kernel, key: str = "k1") -> dict[str, Any]:
    record = create(kernel, key)
    assert move(kernel, record, "triage", args={"priority": 1})[0] == "succeeded"
    return record


def opened_change(kernel: Kernel, record_id: str, agent: str = "a1") -> str:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"change-{record_id[-6:]}-{agent}-{time.monotonic_ns()}",
            "agent_id": agent,
            "namespace": "test",
            "objective": "Open a change",
            "operation": "vcs.open_change",
            "arguments": {"record_id": record_id, "title": "fix"},
            "grants": ["objects.read", "objects.write", "vcs.open_change"],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_operation(task_id, "execute").receipt_id


def test_acquire_holder_and_release_effects(work: Kernel) -> None:
    record = ready(work)
    _, started, _ = move(work, record, "start")
    assert started is not None
    assert started["record"]["holder"] == "a1" and started["claim"]["claim_epoch"] == 1
    assert stored(work, record["record_id"])["claim_transition"] == "start"
    other = ready(work, "k2")
    assert move(work, other, "start", agent="a1")[2] == "claim_limit"  # max_per_holder 1
    receipt = opened_change(work, record["record_id"])
    # claim: holder needs the holder and its current epoch.
    assert move(work, record, "submit", agent="a2", receipt_id=receipt)[2] == "claim_lost"
    assert move(work, record, "submit", receipt_id=receipt)[2] == "claim_lost"
    _, submitted, _ = move(work, record, "submit", claim_epoch=1, receipt_id=receipt)
    assert submitted is not None and submitted["record"]["state"] == "review"
    # The holder cannot approve its own work (guard); a reviewer releases the claim.
    assert move(work, record, "approve", claim_epoch=1)[2] == "guard_refused"
    _, done, _ = move(work, record, "approve", agent="a2")
    assert done is not None and done["record"]["state"] == "done"
    assert done["record"]["holder"] is None and "claim" not in done
    kinds = [e[0] for e in events(work, record["record_id"])]
    assert kinds == ["created", "transitioned", "transitioned", "transitioned", "transitioned"]


def test_an_expired_acquired_claim_returns_to_on_claim_expiry(work: Kernel) -> None:
    record = ready(work)
    move(work, record, "start")
    lease = stored(work, record["record_id"])["lease_expires_at"]
    from datetime import datetime

    after = datetime.fromisoformat(lease) + timedelta(seconds=60)
    Scheduler(work, lambda: after).tick()
    row = stored(work, record["record_id"])
    assert (row["state"], row["holder"], row["claim_epoch"]) == ("ready", None, 2)
    expired = journal(work, "claim_expired")[-1]
    assert expired["data"]["from"] == "doing" and expired["data"]["to"] == "ready"
    # The old holder is fenced; the next one starts a new epoch.
    assert move(work, record, "submit", claim_epoch=1)[2] == "transition_not_allowed"
    _, again, _ = move(work, record, "start", agent="a2")
    assert again is not None and again["claim"]["claim_epoch"] == 3


# Receipt evidence


def test_submit_needs_a_receipt_for_this_record_by_the_caller(work: Kernel) -> None:
    record = ready(work)
    move(work, record, "start")
    assert move(work, record, "submit", claim_epoch=1)[2] == "receipt_required"
    elsewhere = ready(work, "k2")
    wrong_record = opened_change(work, elsewhere["record_id"])
    assert move(work, record, "submit", claim_epoch=1, receipt_id=wrong_record)[2] == (
        "receipt_invalid"
    )
    by_other = opened_change(work, record["record_id"], agent="a2")
    assert move(work, record, "submit", claim_epoch=1, receipt_id=by_other)[2] == (
        "receipt_invalid"
    )
    not_vcs = create(work, "k3")  # a records.create receipt is not a vcs.open_change one
    with work.database.read() as conn:
        create_receipt = conn.exec_driver_sql(
            "SELECT receipt_id FROM record_events WHERE record_id=?", (not_vcs["record_id"],)
        ).scalar_one()
    assert move(work, record, "submit", claim_epoch=1, receipt_id=create_receipt)[2] == (
        "receipt_invalid"
    )
    mine = opened_change(work, record["record_id"])
    assert move(work, record, "submit", claim_epoch=1, receipt_id=mine)[0] == "succeeded"


# Changing the table


def test_removing_an_occupied_state_needs_migrate(work: Kernel) -> None:
    record = ready(work)
    renamed = {
        **WORK_SPEC,
        "states": ["open", "queued", "doing", "review", "done"],
        "transitions": [
            {"name": "triage", "from": ["open"], "to": "queued", "guard": "args.priority > 0"},
            {
                "name": "start",
                "from": ["queued"],
                "to": "doing",
                "claim": "acquire",
                "on_claim_expiry": "queued",
            },
        ],
    }
    refused = apply(work, "work", renamed)
    assert refused.status_code == 409 and refused.json()["code"] == "state_occupied"
    migrating = {**renamed, "migrate": [{"from": "ready", "to": "queued"}]}
    plan = apply(work, "work", migrating, dry_run=True).json()["plan"]
    assert plan["migrated"] == [{"from": "ready", "to": "queued", "records": 1}]
    assert stored(work, record["record_id"])["state"] == "ready"
    applied = apply(work, "work", migrating).json()
    assert applied["metadata"]["generation"] == 2
    row = stored(work, record["record_id"])
    assert row["state"] == "queued"
    assert [e[0] for e in events(work, record["record_id"])][-1] == "migrated"
    assert journal(work, "migrated")[-1]["data"]["to"] == "queued"
    assert move(work, record, "start")[0] == "succeeded"


def test_a_collection_that_gains_states_adopts_its_records(kernel: Kernel) -> None:
    plain = {"supervisor": "supervisor", "schema": {"type": "object"}}
    apply(kernel, "work", plain)
    record = create(kernel)
    assert record["state"] is None
    applied = apply(kernel, "work", WORK_SPEC).json()
    assert applied["plan"]["migrated"] == [{"from": None, "to": "open", "records": 1}]
    assert stored(kernel, record["record_id"])["state"] == "open"
    refused = apply(kernel, "work", plain)
    assert refused.json()["code"] == "state_occupied"


# Approval gates (decision 3.2, ADR-036 amendment of 2026-11-09)

ORDERS_SPEC: dict[str, Any] = {
    "supervisor": "supervisor",
    "schema": {"type": "object"},
    "states": ["proposed", "approved"],
    "initial": "proposed",
    "terminal": ["approved"],
    "transitions": [
        {
            "name": "approve",
            "from": ["proposed"],
            "to": "approved",
            "approval": {"level": "always", "reason": "Every order needs a desk approver"},
        }
    ],
}


def order(kernel: Kernel) -> tuple[str, dict[str, Any]]:
    """A proposed order and a task asking to approve it, which waits for a decision."""
    assert apply(kernel, "orders", ORDERS_SPEC).status_code == 200
    state, output, code = run(
        kernel,
        "records.create",
        {"collection": "orders", "client_key": "o1", "data": {"qty": 1}},
        grants=ORDERS,
    )
    assert output is not None, code
    record = output["record"]
    request = OperationTaskRequest.model_validate(
        {
            "request_id": "approve-o1",
            "agent_id": "a1",
            "namespace": "test",
            "objective": "Approve the order",
            "operation": "records.transition",
            "arguments": {
                "collection": "orders",
                "record_id": record["record_id"],
                "transition": "approve",
            },
            "grants": ["objects.read", "objects.write", *ORDERS],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    assert OperationRuntime(kernel).execute(task_id) == "waiting"
    return task_id, record


def pending(kernel: Kernel, task_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        return dict(
            conn.exec_driver_sql("SELECT * FROM approval_requests WHERE task_id=?", (task_id,))
            .mappings()
            .one()
        )


def decide(kernel: Kernel, token: str, row: dict[str, Any]) -> Any:
    return TestClient(create_app(kernel)).post(
        f"/v0.2/approvals/{row['request_id']}/decision",
        json={
            "decision_id": f"{token}-1",
            "decision": "approve",
            "intent_digest": row["intent_digest"],
        },
        headers={"Authorization": f"Bearer {token}", "X-Cardea-Auth": "spiffe"},
    )


def test_an_approval_gate_waits_for_the_resource_qualified_approver(kernel: Kernel) -> None:
    task_id, record = order(kernel)
    row = pending(kernel, task_id)
    assert row["approval_resource"] == "record:orders/approve"
    assert row["operation"] == "records.transition"
    assert stored(kernel, record["record_id"])["state"] == "proposed"
    # Operation-level approve on records.transition does not decide a transition gate.
    assert decide(kernel, "operator", row).json()["code"] == "approver_denied"
    assert decide(kernel, "outsider", row).json()["code"] == "approver_denied"
    decided = decide(kernel, "approver", row)
    assert decided.status_code == 200, decided.json()
    assert OperationRuntime(kernel).execute(task_id, approval=row["request_id"]) == "succeeded"
    assert stored(kernel, record["record_id"])["state"] == "approved"
    with kernel.database.read() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "apr-01")
    block = cast(Any, receipt.request.arguments)["context"]["approval"]
    assert block["approver_id"] == "approver"


def test_approve_on_one_collection_does_not_decide_another(kernel: Kernel) -> None:
    task_id, _ = order(kernel)
    row = pending(kernel, task_id)
    # The approver holds approve on record:orders/approve only.
    forged = {**row, "approval_resource": "record:work/approve"}
    from cardea.service.approval_requests import holds_approve
    from cardea.service.policy import Policy

    with kernel.database.read() as conn:
        assert holds_approve(Policy(conn), "approver", cast(Any, row))
        assert not holds_approve(Policy(conn), "approver", cast(Any, forged))


def test_only_the_gated_transition_requires_approval(kernel: Kernel) -> None:
    """The transition's gate compiles to a rule on records.transition for that collection and
    transition only; agent-run waits are in test_worked_examples (ADR-036 slice 4)."""
    from cardea.service.approval_requests import evaluate

    assert apply(kernel, "orders", ORDERS_SPEC).status_code == 200
    with kernel.database.read() as conn:
        needed = evaluate(
            conn,
            kernel.operations,
            "records.transition",
            "test",
            {"collection": "orders", "record_id": "rec-" + "0" * 32, "transition": "approve"},
        )
        free = evaluate(
            conn,
            kernel.operations,
            "records.transition",
            "test",
            {"collection": "work", "record_id": "rec-" + "0" * 32, "transition": "approve"},
        )
    assert needed.required and [r.rule_id for r in needed.rules] == ["transition.approve"]
    assert not free.required


def test_epoch_evidence_counts_only_for_the_current_claim(work: Kernel) -> None:
    """Evidence named by `<record_id>.<epoch>` must match the record's current claim epoch,
    so a holder that lost and reacquired the record cannot cite its earlier work."""
    spec = {
        **WORK_SPEC,
        "transitions": [
            *(t for t in WORK_SPEC["transitions"] if t["name"] != "submit"),
            {
                "name": "submit",
                "from": ["doing"],
                "to": "review",
                "claim": "holder",
                "requires_receipt": {
                    "operation": "ci.report",
                    "in": "same_caller",
                    "window_seconds": 3600,
                },
            },
        ],
    }
    assert apply(work, "work", spec).status_code == 200
    record = ready(work)
    rid = record["record_id"]

    def report(epoch: int) -> str:
        request = OperationTaskRequest.model_validate(
            {
                "request_id": f"ci-{epoch}",
                "agent_id": "a1",
                "namespace": "test",
                "objective": "Report CI",
                "operation": "ci.report",
                "idempotency_context": f"{rid}.{epoch}",
                "grants": ["objects.read", "objects.write", "ci.report"],
            }
        )
        task_id = work.submit(work.auth.validate("supervisor"), request).task.task_id
        assert OperationRuntime(work).execute(task_id) == "succeeded"
        with work.database.read() as conn, borrow(conn) as tx:
            return tx.get_operation(task_id, "execute").receipt_id

    move(work, record, "start")
    early = report(1)
    from datetime import datetime

    lease = datetime.fromisoformat(stored(work, rid)["lease_expires_at"])
    Scheduler(work, lambda: lease + timedelta(seconds=60)).tick()
    _, again, _ = move(work, record, "start")
    assert again is not None and again["claim"]["claim_epoch"] == 3
    stale = move(work, record, "submit", claim_epoch=3, receipt_id=early)
    assert stale[2] == "receipt_invalid"
    current = report(3)
    assert move(work, record, "submit", claim_epoch=3, receipt_id=current)[0] == "succeeded"


def test_summary_lists_what_the_callers_transitions_can_move(work: Kernel) -> None:
    """`records.summary` counts by state; `available` follows the caller's live
    transition grants (ADR-034 slice 6)."""
    first, second = create(work, "k1"), create(work, "k2")
    assert move(work, first, "triage", args={"priority": 1})[0] == "succeeded"
    every = run(work, "records.summary", {"collection": "work"}, grants=ALL)
    assert every[0] == "succeeded" and every[1] is not None
    assert every[1]["by_state"] == {"open": 1, "ready": 1} and every[1]["total"] == 2
    assert {a["record_id"] for a in every[1]["available"]} == {
        first["record_id"],
        second["record_id"],
    }
    starter = ("record:work/read", "record:work/start")
    only = run(work, "records.summary", {"collection": "work"}, grants=starter)
    assert only[1] is not None
    assert [(a["record_id"], a["state"]) for a in only[1]["available"]] == [
        (first["record_id"], "ready")
    ]
    reader = run(work, "records.summary", {"collection": "work"}, grants=("record:work/read",))
    assert reader[1] is not None and reader[1]["available"] == []


# Receipt-result guards (ADR-034 amendment, 2026-11-16; ADR-038 question 3)


def guarded_spec(result: str | None = "result.passed == true") -> dict[str, Any]:
    needs: dict[str, Any] = {"operation": "ci.run", "in": "same_caller", "window_seconds": 3600}
    if result is not None:
        needs["result"] = result
    return {
        **WORK_SPEC,
        "transitions": [
            *(t for t in WORK_SPEC["transitions"] if t["name"] != "submit"),
            {
                "name": "submit",
                "from": ["doing"],
                "to": "review",
                "claim": "holder",
                "requires_receipt": needs,
            },
        ],
    }


def ci_receipt(kernel: Kernel, record_id: str, passed: bool, **extra: Any) -> str:
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"ci-run-{record_id[-6:]}-{time.monotonic_ns()}",
            "agent_id": "a1",
            "namespace": "test",
            "objective": "Run the checks",
            "operation": "ci.run",
            "arguments": {"record_id": record_id, "passed": passed, **extra},
            "grants": ["objects.read", "objects.write", "ci.run"],
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    assert OperationRuntime(kernel).execute(task_id) == "succeeded"
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_operation(task_id, "execute").receipt_id


def test_a_transition_needs_a_passing_cited_result(work: Kernel) -> None:
    assert apply(work, "work", guarded_spec()).status_code == 200
    record = ready(work)
    move(work, record, "start")
    failing = ci_receipt(work, record["record_id"], passed=False)
    refused = move(work, record, "submit", claim_epoch=1, receipt_id=failing)
    assert refused[0] == "failed" and refused[2] == "receipt_result_refused"
    passing = ci_receipt(work, record["record_id"], passed=True)
    assert move(work, record, "submit", claim_epoch=1, receipt_id=passing)[0] == "succeeded"


def test_a_guard_that_cannot_evaluate_refuses(work: Kernel) -> None:
    assert apply(work, "work", guarded_spec("result.missing == 1")).status_code == 200
    record = ready(work)
    move(work, record, "start")
    cited = ci_receipt(work, record["record_id"], passed=True)
    assert move(work, record, "submit", claim_epoch=1, receipt_id=cited)[2] == (
        "receipt_result_refused"
    )


@pytest.mark.parametrize(
    "source", ["record.state == 'open'", "args.x == 1", "result.scenarios.all(s, s.ok)", ""]
)
def test_a_result_guard_reads_only_the_result_without_macros(work: Kernel, source: str) -> None:
    response = apply(work, "work", guarded_spec(source))
    assert response.status_code == 422, response.json()


def test_a_requirement_without_a_result_guard_keeps_its_digest() -> None:
    plain = CollectionDocument.model_validate(
        {"metadata": {"name": "work", "namespace": "test"}, "spec": guarded_spec(None)}
    )
    guarded = CollectionDocument.model_validate(
        {"metadata": {"name": "work", "namespace": "test"}, "spec": guarded_spec()}
    )
    stored = plain.spec_json()["transitions"]
    assert isinstance(stored, list)
    assert all("result" not in json.dumps(t) for t in stored)
    assert plain.digest != guarded.digest


def recording_spec(
    sets: dict[str, str] | None, schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    spec = guarded_spec(None)
    spec["schema"] = schema or {
        "type": "object",
        "properties": {"title": {"type": "string"}, "passed": {"type": "boolean"}},
    }
    if sets is not None:
        spec["transitions"][-1]["requires_receipt"]["sets"] = sets
    return spec


def test_a_transition_records_values_from_its_cited_receipt(work: Kernel) -> None:
    assert apply(work, "work", recording_spec({"passed": "/passed"})).status_code == 200
    record = ready(work)
    move(work, record, "start")
    cited = ci_receipt(work, record["record_id"], passed=True)
    state, output, _ = move(work, record, "submit", claim_epoch=1, receipt_id=cited)
    assert state == "succeeded" and output is not None
    assert output["record"]["data"] == {"title": "k1", "passed": True}
    assert json.loads(stored(work, record["record_id"])["data_json"]) == {
        "title": "k1",
        "passed": True,
    }


def test_a_recorded_value_must_exist_and_match_the_schema(work: Kernel) -> None:
    assert apply(work, "work", recording_spec({"passed": "/missing"})).status_code == 200
    record = ready(work)
    move(work, record, "start")
    cited = ci_receipt(work, record["record_id"], passed=True)
    missing = move(work, record, "submit", claim_epoch=1, receipt_id=cited)
    assert missing[0] == "failed" and missing[2] == "receipt_result_refused"
    # The schema still applies: a boolean is not a string.
    mismatched = recording_spec(
        {"passed": "/passed"},
        {
            "type": "object",
            "properties": {"title": {"type": "string"}, "passed": {"type": "string"}},
        },
    )
    assert apply(work, "work", mismatched).status_code == 200
    refused = move(work, record, "submit", claim_epoch=1, receipt_id=cited)
    assert refused[0] == "failed" and refused[2] == "record_invalid"
    assert stored(work, record["record_id"])["state"] == "doing"


def test_a_recorded_field_must_be_declared_at_apply(work: Kernel) -> None:
    response = apply(work, "work", recording_spec({"undeclared": "/passed"}))
    assert response.status_code == 422, response.json()


def test_a_requirement_without_sets_keeps_its_digest() -> None:
    plain = CollectionDocument.model_validate(
        {"metadata": {"name": "work", "namespace": "test"}, "spec": recording_spec(None)}
    )
    recording = CollectionDocument.model_validate(
        {
            "metadata": {"name": "work", "namespace": "test"},
            "spec": recording_spec({"passed": "/passed"}),
        }
    )
    assert all("sets" not in json.dumps(t) for t in plain.spec_json()["transitions"])  # type: ignore[union-attr]
    assert plain.digest != recording.digest
