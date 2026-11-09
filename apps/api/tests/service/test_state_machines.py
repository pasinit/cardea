"""Record state machines (ADR-034 slice 3): the transition table, guards, claim effects,
receipt evidence, migration and approval gates; portable fakes only."""

import json
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
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
from cardea.service.operations import OperationContext, OperationDescriptor, Registry
from cardea.service.records import CollectionDocument
from cardea.service.triggers import Scheduler

TOKENS = {"runtime", "supervisor", "approver", "operator", "outsider"}
HOUR = timedelta(hours=1)
WORK_ACTIONS = ("create", "read", "update", "triage", "start", "submit", "rework", "approve")
WORK = tuple(f"record:work/{a}" for a in (*WORK_ACTIONS, "broken"))
ORDERS = ("record:orders/create", "record:orders/read", "record:orders/approve")
SUPERVISOR = (*RIGHTS, "groups.manage", "vcs.open_change", *WORK, *ORDERS)


class Auth:
    def runtime_proof(self) -> Proof:
        return Proof("spiffe", "test", "runtime", time.time() + 60)

    def validate(self, token: str, kind: str = "spiffe") -> Proof:
        if token not in TOKENS:
            raise Fault("invalid_credential", "Unknown test credential", 401)
        return Proof("spiffe", "test", token, time.time() + 60)


async def open_change(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    return {"change_id": f"change-{context.receipt_id[-8:]}"}


VCS = OperationDescriptor.model_validate(
    {
        "tool": {
            "name": "vcs.open_change",
            "inputSchema": {
                "type": "object",
                "properties": {"record_id": {"type": "string"}, "title": {"type": "string"}},
                "required": ["record_id"],
            },
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.vcs"},
    }
)


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
        {"principal_id": "approver", "operation": "record:orders/approve", "allow": False,
         "approve": True, **grant},
        # Operation-level approve on records.transition: must not satisfy a transition gate.
        {"principal_id": "operator", "operation": "records.transition", "allow": False,
         "approve": True, **grant},
    ]
    platform = [
        {"principal_id": "platform", "operation": r, **grant}
        for r in supervisor
        if r != "record:orders/approve"
    ] + [
        {"principal_id": "platform", "operation": "record:orders/approve", "approve": True,
         **grant},
        {"principal_id": "platform", "operation": "records.transition", "allow": False,
         "approve": True, **grant},
    ]
    return Provisioning.model_validate(
        {"principals": principals, "permissions": permissions, "platform": platform}
    )


REGISTRY = Registry((VCS,), {"test.vcs": open_change})


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
        {"name": "start", "from": ["ready"], "to": "doing", "claim": "acquire",
         "on_claim_expiry": "ready"},
        {"name": "submit", "from": ["doing"], "to": "review", "claim": "holder",
         "requires_receipt": {"operation": "vcs.open_change", "in": "same_caller",
                              "window_seconds": 3600}},
        {"name": "rework", "from": ["review"], "to": "doing",
         "guard": "caller.id != record.created_by"},
        {"name": "approve", "from": ["review"], "to": "done", "claim": "release",
         "guard": "caller.id != record.holder"},
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
        {"transitions": [{"name": "x", "from": ["open"], "to": "ready",
                          "on_claim_expiry": "open"}]},  # needs acquire
        {"transitions": [{"name": "x", "from": ["open"], "to": "ready", "guard": "ns == 1"}]},
        {"transitions": [{"name": "x", "from": ["open"], "to": "ready",
                          "approval": {"level": "conditional", "when": "a" * 520,
                                       "reason": "too long"}}]},
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
    assert found is not None and [r["record_id"] for r in found["records"]] == [
        record["record_id"]
    ]


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
