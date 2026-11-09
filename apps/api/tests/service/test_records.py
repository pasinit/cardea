"""Collections and records (ADR-034 slice 1); portable fakes only."""

import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from cardea.service import kernel as handlers
from cardea.service import operation_runtime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault, borrow
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
from cardea.service.operations import OperationContext, Registry
from cardea.service.records import CollectionDocument


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


RECORD_GRANTS = ("record:items/create", "record:items/read", "record:items/update")
SUPERVISOR = (*RIGHTS, "groups.manage", "models.invoke", *RECORD_GRANTS)
PRINCIPALS = tuple(
    Principal(
        principal_id=p,
        kind="service" if p == "runtime" else "human",
        auth_kind="spiffe",
        issuer="test",
        subject=p,
    )
    for p in ("runtime", "supervisor", "outsider")
) + tuple(Principal(principal_id=a, kind="agent", runtime_id="runtime") for a in ("a1", "a2"))


def document(rights: tuple[str, ...] = SUPERVISOR) -> Provisioning:
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in rights
    )
    return Provisioning(
        principals=PRINCIPALS,
        permissions=granted,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
    )


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    db = Database(tmp_path / "data")
    db.migrate()
    registry = Registry((), {})
    provision(db, document(), registry)
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        db.close()


SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "owner": {"type": "string"}},
    "required": ["title"],
}


def collection(**spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "Collection",
        "metadata": {"name": "items", "namespace": "test"},
        "spec": {"supervisor": "supervisor", "schema": SCHEMA, **spec},
    }


HEADERS = {"Authorization": "Bearer supervisor", "X-Cardea-Auth": "spiffe"}


def put(kernel: Kernel, body: dict[str, Any], token: str = "supervisor", **params: Any) -> Any:
    client = TestClient(create_app(kernel))
    return client.put(
        f"/v0.2/collections/{body['metadata']['name']}",
        json=body,
        params=params,
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
    )


Result = tuple[str, dict[str, Any] | None, str | None]
SEQUENCE = iter(range(10**6))


def run(
    kernel: Kernel,
    operation: str,
    arguments: dict[str, Any],
    *,
    agent: str = "a1",
    grants: tuple[str, ...] = RECORD_GRANTS,
    context: str | None = None,
) -> Result:
    """One operation task; returns (task state, output, failure code)."""
    request = OperationTaskRequest.model_validate(
        {
            "request_id": f"r-{next(SEQUENCE)}",
            "agent_id": agent,
            "namespace": "test",
            "objective": f"call {operation}",
            "operation": operation,
            "arguments": arguments,
            "grants": ["objects.read", "objects.write", *grants],
            **({"idempotency_context": context} if context else {}),
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    state = OperationRuntime(kernel).execute(task_id)
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
    output = receipt.result.get("output") if receipt.result else None
    code = receipt.failure.code if receipt.failure else None
    return state, cast(dict[str, Any] | None, output), code


def create(kernel: Kernel, key: str = "k1", **changes: Any) -> Result:
    return run(
        kernel,
        "records.create",
        {"collection": "items", "client_key": key, "data": {"title": "first"}, **changes},
    )


def created(kernel: Kernel, key: str = "k1", **changes: Any) -> dict[str, Any]:
    state, output, code = create(kernel, key, **changes)
    assert state == "succeeded", code
    assert output is not None
    return cast(dict[str, Any], output["record"])


def update(kernel: Kernel, record: dict[str, Any], version: int, **changes: Any) -> Result:
    agent = changes.pop("agent", "a1")
    return run(
        kernel,
        "records.update",
        {
            "collection": "items",
            "record_id": record["record_id"],
            "expected_version": version,
            **changes,
        },
        agent=agent,
    )


def stored(kernel: Kernel, record_id: str) -> sa.Row[Any]:
    with kernel.database.read() as conn:
        return conn.exec_driver_sql(
            "SELECT version,data_json FROM records WHERE record_id=?", (record_id,)
        ).one()


def events(kernel: Kernel, record_id: str) -> list[sa.Row[Any]]:
    with kernel.database.read() as conn:
        return list(
            conn.exec_driver_sql(
                "SELECT kind,version,actor,receipt_id FROM record_events WHERE record_id=? "
                "ORDER BY event_id",
                (record_id,),
            )
        )


@pytest.fixture
def items(kernel: Kernel) -> Kernel:
    assert put(kernel, collection()).status_code == 200
    return kernel


# Collection documents


def test_apply_tracks_generation_and_dry_run_writes_nothing(kernel: Kernel) -> None:
    planned = put(kernel, collection(), dry_run="true")
    assert planned.status_code == 200, planned.json()
    assert planned.json()["plan"] == {"change": "created", "generation": 1, "dry_run": True}
    client = TestClient(create_app(kernel))
    assert client.get("/v0.2/collections/items?namespace=test", headers=HEADERS).status_code == 404
    first = put(kernel, collection()).json()
    assert first["plan"]["change"] == "created"
    assert first["metadata"]["generation"] == 1
    assert first["status"] == {"observedGeneration": 1, "records": 0}
    assert put(kernel, collection()).json()["plan"]["change"] == "unchanged"
    changed = put(kernel, collection(max_record_bytes=1024)).json()
    assert changed["plan"] == {"change": "updated", "generation": 2, "dry_run": False}
    view = client.get("/v0.2/collections/items?namespace=test", headers=HEADERS).json()
    assert view["metadata"]["generation"] == 2 and view["spec"]["max_record_bytes"] == 1024
    assert view["spec"]["schema"] == SCHEMA


def test_apply_requires_the_supervisor_and_groups_manage(kernel: Kernel) -> None:
    assert put(kernel, collection(), token="outsider").json()["code"] == "collection_supervisor"
    other = collection()
    other["spec"]["supervisor"] = "outsider"
    assert put(kernel, other, token="outsider").json()["code"] == "authority_denied"
    provision(kernel.database, document(tuple(r for r in SUPERVISOR if r != "groups.manage")))
    assert put(kernel, collection()).json()["code"] == "authority_denied"


def test_get_needs_supervision_or_a_read_grant(items: Kernel) -> None:
    client = TestClient(create_app(items))
    outsider = {**HEADERS, "Authorization": "Bearer outsider"}
    assert client.get("/v0.2/collections/items?namespace=test", headers=outsider).status_code == 404


@pytest.mark.parametrize(
    "spec",
    [
        {"schema": {"type": "array"}},
        {"schema": {"type": "object", "properties": {"x": {"type": 5}}}},
        {"guards": {"update": "record.labels.exists(l, l == 'x')"}},  # comprehension macro
        {"guards": {"update": "ns == 'test'"}},  # not a guard variable
        {"guards": {"update": "args.a == 1 || " * 40 + "true"}},  # over 512 characters
        {"guards": {"claim": "true"}},  # a later slice
        {"states": ["open"]},  # state machines are slice 3
        {"claims": {"lease_seconds": 3600}},  # claims are slice 2
        {"max_record_bytes": 10**6},
    ],
)
def test_invalid_collections_are_refused(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CollectionDocument.model_validate(collection(**spec))


def test_path_and_body_names_must_agree(kernel: Kernel) -> None:
    client = TestClient(create_app(kernel))
    response = client.put("/v0.2/collections/other", json=collection(), headers=HEADERS)
    assert response.json()["code"] == "collection_mismatch"


# Records


def test_create_get_and_query(items: Kernel) -> None:
    first = created(items, labels=["triage", "p1"], links=[{"rel": "issue", "external_id": "42"}])
    assert first["version"] == 1 and first["created_by"] == "a1" and first["state"] is None
    assert first["holder"] is None and first["claim_epoch"] == 0
    second = created(items, "k2", data={"title": "second", "owner": "a2"}, labels=["p1"])
    state, output, _ = run(
        items, "records.get", {"collection": "items", "record_id": first["record_id"]}
    )
    assert state == "succeeded" and output == {"record": first}
    _, by_label, _ = run(items, "records.query", {"collection": "items", "label": "triage"})
    assert by_label is not None and [r["record_id"] for r in by_label["records"]] == [
        first["record_id"]
    ]
    _, by_field, _ = run(items, "records.query", {"collection": "items", "where": {"owner": "a2"}})
    assert by_field is not None and [r["record_id"] for r in by_field["records"]] == [
        second["record_id"]
    ]
    _, unheld, _ = run(items, "records.query", {"collection": "items", "holder": None})
    assert unheld is not None and len(unheld["records"]) == 2
    _, page, _ = run(items, "records.query", {"collection": "items", "limit": 1})
    assert page is not None and page["records"][0]["record_id"] == first["record_id"]
    _, rest, _ = run(
        items, "records.query", {"collection": "items", "limit": 1, "cursor": page["next_cursor"]}
    )
    assert rest is not None and rest["records"][0]["record_id"] == second["record_id"]
    assert rest["next_cursor"] is None


def test_missing_collection_and_record_are_refused(kernel: Kernel) -> None:
    assert run(kernel, "records.query", {"collection": "items"})[2] == "collection_not_found"
    put(kernel, collection())
    missing = {"collection": "items", "record_id": "rec-" + "0" * 32}
    assert run(kernel, "records.get", missing)[2] == "record_not_found"


# Acceptance: schema validation


def test_data_must_match_the_schema_and_size(items: Kernel) -> None:
    assert create(items, data={"owner": "a1"})[2] == "record_invalid"
    assert create(items, data={"title": 7})[2] == "record_invalid"
    put(items, collection(max_record_bytes=64))
    assert create(items, data={"title": "x" * 100})[2] == "record_too_large"
    put(items, collection())
    record = created(items)
    assert update(items, record, 1, data={"title": None})[2] == "record_invalid"
    with pytest.raises(Fault) as malformed:
        update(items, record, 1, labels=["bad label"])
    assert malformed.value.code == "invalid_arguments"
    assert stored(items, record["record_id"])[0] == 1
    assert [e[0] for e in events(items, record["record_id"])] == ["created"]
    with items.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM records").scalar_one() == 1


# Acceptance: compare-and-set


def test_update_is_compare_and_set_on_version(items: Kernel) -> None:
    record = created(items)
    state, output, _ = update(items, record, 1, data={"title": "edited"}, labels=["done"])
    assert state == "succeeded" and output is not None
    assert output["record"]["version"] == 2 and output["record"]["labels"] == ["done"]
    # A writer that read version 1 loses; nothing changes.
    state, _, code = update(items, record, 1, data={"title": "stale"})
    assert (state, code) == ("failed", "version_conflict")
    assert stored(items, record["record_id"]) == (2, '{"title": "edited"}')
    assert [e[1] for e in events(items, record["record_id"])] == [1, 2]


# Acceptance: guards, failing closed


def test_guards_narrow_writes_and_fail_closed(items: Kernel) -> None:
    put(
        items,
        collection(
            guards={
                "create": "args.data.title != 'forbidden'",
                "update": "record.created_by == caller.id",
            }
        ),
    )
    assert create(items, data={"title": "forbidden"})[2] == "guard_refused"
    record = created(items)
    assert update(items, record, 1, agent="a2", data={"title": "x"})[2] == "guard_refused"
    assert update(items, record, 1, data={"title": "mine"})[0] == "succeeded"
    # An evaluation error (a missing field) refuses, as does a non-boolean result.
    put(items, collection(guards={"update": "record.data.missing == 'x'"}))
    assert update(items, record, 2, data={"title": "y"})[2] == "guard_refused"
    put(items, collection(guards={"update": "caller.pool == 'dev' || caller.group != null"}))
    assert update(items, record, 2, data={"title": "y"})[2] == "guard_refused"
    assert stored(items, record["record_id"])[0] == 2


# Acceptance: append-only history


def test_history_is_append_only_and_served(items: Kernel) -> None:
    record = created(items)
    update(items, record, 1, data={"title": "edited"})
    with items.database.engine.begin() as conn:
        for statement in (
            "UPDATE record_events SET actor='forged'",
            "DELETE FROM record_events",
            "DELETE FROM records",
        ):
            with pytest.raises(sa.exc.DatabaseError):
                conn.exec_driver_sql(statement)
    client = TestClient(create_app(items))
    response = client.get(
        f"/v0.2/collections/items/records/{record['record_id']}/events?namespace=test",
        headers=HEADERS,
    )
    history = response.json()
    assert [e["kind"] for e in history["events"]] == ["created", "updated"]
    assert history["events"][0]["old"] is None
    assert history["events"][1]["old"]["data"] == {"title": "first"}
    assert history["events"][1]["new"]["data"] == {"title": "edited"}
    assert all(e["actor"] == "a1" and e["receipt_id"] for e in history["events"])
    outsider = {**HEADERS, "Authorization": "Bearer outsider"}
    url = f"/v0.2/collections/items/records/{record['record_id']}/events?namespace=test"
    assert client.get(url, headers=outsider).status_code == 404


# Acceptance: grants under the live ceiling


def test_each_call_needs_its_record_grant(items: Kernel) -> None:
    record = created(items)
    # The task holds read only: an update is refused at the call, before any write.
    state, _, code = update_with(items, record, ("record:items/read",))
    assert (state, code) == ("failed", "task_grant_denied")
    # A grant on another collection does not cover this one.
    provision(items.database, document((*SUPERVISOR, "record:other/update")))
    assert update_with(items, record, ("record:other/update",))[2] == "task_grant_denied"
    assert stored(items, record["record_id"])[0] == 1


def test_supervisor_cannot_delegate_a_record_grant_it_lacks(items: Kernel) -> None:
    provision(items.database, document(tuple(r for r in SUPERVISOR if r != "record:items/update")))
    with pytest.raises(Fault) as refused:
        update(items, {"record_id": "rec-" + "0" * 32}, 1, data={"title": "x"})
    assert refused.value.code == "authority_denied"


def test_revocation_stops_the_next_call(items: Kernel) -> None:
    record = created(items)
    request = OperationTaskRequest.model_validate(
        {
            "request_id": "revoked",
            "agent_id": "a1",
            "namespace": "test",
            "objective": "update",
            "operation": "records.update",
            "arguments": {
                "collection": "items",
                "record_id": record["record_id"],
                "expected_version": 1,
                "data": {"title": "late"},
            },
            "grants": ["objects.read", "objects.write", *RECORD_GRANTS],
        }
    )
    task_id = items.submit(items.auth.validate("supervisor"), request).task.task_id
    provision(items.database, document(tuple(r for r in SUPERVISOR if r != "record:items/update")))
    assert OperationRuntime(items).execute(task_id) == "failed"
    with items.database.engine.begin() as conn, borrow(conn) as tx:
        receipt = tx.get_operation(task_id, "execute")
    assert receipt.failure is not None and receipt.failure.code == "authority_denied"
    assert stored(items, record["record_id"])[0] == 1


def update_with(kernel: Kernel, record: dict[str, Any], grants: tuple[str, ...]) -> Result:
    return run(
        kernel,
        "records.update",
        {
            "collection": "items",
            "record_id": record["record_id"],
            "expected_version": 1,
            "data": {"title": "x"},
        },
        grants=grants,
    )


# Acceptance: idempotent retried create (ADR-026 scope on /collection, /client_key)


def test_a_retried_create_returns_the_first_record(items: Kernel) -> None:
    first = run(
        items,
        "records.create",
        {"collection": "items", "client_key": "slot-1", "data": {"title": "once"}},
        context="trigger.20261109T090000Z",
    )
    retry = run(
        items,
        "records.create",
        {"collection": "items", "client_key": "slot-1", "data": {"title": "reworded"}},
        context="trigger.20261109T090000Z",
    )
    assert first[0] == retry[0] == "succeeded"
    assert first[1] == retry[1]
    other = run(
        items,
        "records.create",
        {"collection": "items", "client_key": "slot-1", "data": {"title": "next slot"}},
        context="trigger.20261110T090000Z",
    )
    assert other[1] is not None and first[1] is not None
    assert other[1]["record"]["record_id"] != first[1]["record"]["record_id"]
    with items.database.read() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM records").scalar_one() == 2


def test_an_interrupted_write_reconciles_from_history(items: Kernel) -> None:
    record = created(items)
    receipt = events(items, record["record_id"])[0][3]
    reconciler = items.reconciler("records.create")
    assert reconciler is not None and items.reconciler("records.get") is None

    def context(receipt_id: str) -> OperationContext:
        return OperationContext("t", "test", receipt_id, now(), receipt_id)

    import asyncio

    applied = asyncio.run(reconciler({}, context(receipt)))
    assert applied.applied and applied.output == {"record": record}
    assert not asyncio.run(reconciler({}, context("receipt-unknown"))).applied
