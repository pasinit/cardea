"""Record claims (ADR-034 slice 2): atomic claims, fencing, renewal, expiry, follow-up
wakes and the apply-time lease bound; portable fakes only."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import test_records as records_test
from test_records import Auth, FakeClient, collection, created, events, put, run

from cardea.service import claims
from cardea.service import kernel as handlers
from cardea.service import operation_runtime
from cardea.service.auth import Authentication
from cardea.service.database import Database
from cardea.service.kernel import Kernel, provision
from cardea.service.models import OperationTaskRequest, now
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import Registry
from cardea.service.triggers import Scheduler

CLAIM_GRANTS = (*records_test.RECORD_GRANTS, "record:items/claim")
CLAIMS = {"lease_seconds": 3600, "max_per_holder": 1, "max_followups": 2, "standalone": True}


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(operation_runtime, "LEASE_MARGIN", 0.3)
    db = Database(tmp_path / "data")
    db.migrate()
    registry = Registry((), {})
    provision(db, records_test.document((*records_test.SUPERVISOR, "record:items/claim")), registry)
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry)
    try:
        yield instance
    finally:
        db.close()


@pytest.fixture
def items(kernel: Kernel) -> Kernel:
    response = put(kernel, collection(claims=CLAIMS))
    assert response.status_code == 200, response.json()
    return kernel


def call(kernel: Kernel, operation: str, record: dict[str, Any], agent: str = "a1", **args: Any) -> Any:
    return run(
        kernel,
        operation,
        {"collection": "items", "record_id": record["record_id"], **args},
        agent=agent,
        grants=CLAIM_GRANTS,
    )


def claim(kernel: Kernel, record: dict[str, Any], agent: str = "a1") -> Any:
    return call(kernel, "records.claim", record, agent)


def row(kernel: Kernel, record_id: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        found = (
            conn.exec_driver_sql("SELECT * FROM records WHERE record_id=?", (record_id,))
            .mappings()
            .one()
        )
    return dict(found)


def lease(kernel: Kernel, record_id: str) -> datetime:
    return datetime.fromisoformat(row(kernel, record_id)["lease_expires_at"])


def journal(kernel: Kernel, event_type: str) -> list[dict[str, Any]]:
    with kernel.database.read() as conn:
        return [
            json.loads(r[0])
            for r in conn.exec_driver_sql(
                "SELECT payload FROM events WHERE event_type=? ORDER BY cursor", (event_type,)
            )
        ]


# Atomic claims


def test_concurrent_claims_have_exactly_one_winner(items: Kernel) -> None:
    record = created(items)
    tasks = []
    for n in range(20):
        request = OperationTaskRequest.model_validate(
            {
                "request_id": f"race-{n}",
                "agent_id": "a1" if n % 2 else "a2",
                "namespace": "test",
                "objective": "claim",
                "operation": "records.claim",
                "arguments": {"collection": "items", "record_id": record["record_id"]},
                "grants": ["objects.read", "objects.write", *CLAIM_GRANTS],
            }
        )
        tasks.append(items.submit(items.auth.validate("supervisor"), request).task.task_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(OperationRuntime(items).execute, tasks))
    claimed = [e for e in events(items, record["record_id"]) if e[0] == "claimed"]
    assert len(claimed) == 1
    winner = claimed[0][2]
    stored = row(items, record["record_id"])
    assert stored["holder"] == winner and stored["claim_epoch"] == 1
    # The winner's other calls are idempotent re-claims; every other call lost.
    assert states.count("succeeded") == 10 and states.count("failed") == 10


def test_claims_need_standalone_config_and_respect_the_holder_limit(kernel: Kernel) -> None:
    put(kernel, collection())
    record = created(kernel)
    assert claim(kernel, record)[2] == "claims_disabled"
    put(kernel, collection(claims={**CLAIMS, "standalone": False}))
    assert claim(kernel, record)[2] == "claims_not_standalone"
    put(kernel, collection(claims=CLAIMS))
    assert claim(kernel, record)[0] == "succeeded"
    other = created(kernel, "k2")
    assert claim(kernel, other)[2] == "claim_limit"
    assert claim(kernel, record, agent="a2")[2] == "record_claimed"


def test_claim_outputs_the_epoch_and_idempotency_context(items: Kernel) -> None:
    record = created(items)
    _, output, _ = claim(items, record)
    assert output["claim"]["claim_epoch"] == 1
    assert output["claim"]["idempotency_context"] == f"{record['record_id']}.1"
    assert output["record"]["holder"] == "a1" and output["record"]["version"] == 2
    _, listed, _ = run(items, "records.claimed", {"collection": "items"}, grants=CLAIM_GRANTS)
    assert [c["record_id"] for c in listed["claims"]] == [record["record_id"]]
    assert listed["claims"][0]["record"]["holder"] == "a1"
    _, others, _ = run(
        items, "records.claimed", {"collection": "items"}, agent="a2", grants=CLAIM_GRANTS
    )
    assert others["claims"] == []


def test_claim_grant_is_required(items: Kernel) -> None:
    record = created(items)
    state, _, code = run(
        items,
        "records.claim",
        {"collection": "items", "record_id": record["record_id"]},
        grants=records_test.RECORD_GRANTS,
    )
    assert (state, code) == ("failed", "task_grant_denied")


# Fencing and expiry


def test_an_expired_claim_returns_to_the_pool_and_fences_the_old_holder(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    ends = lease(items, record["record_id"])
    # A crashed holder renews nothing: the first tick after the lease releases the record.
    Scheduler(items, lambda: ends - timedelta(seconds=1)).tick()
    assert row(items, record["record_id"])["holder"] == "a1"
    Scheduler(items, lambda: ends + timedelta(seconds=60)).tick()
    stored = row(items, record["record_id"])
    assert stored["holder"] is None and stored["lease_expires_at"] is None
    assert stored["claim_epoch"] == 2
    assert [e[0] for e in events(items, record["record_id"])][-1] == "claim_expired"
    assert journal(items, "record.claim_expired")[-1]["reason"] == "lease_expired"
    # The old holder is fenced at its next holder-only call.
    assert call(items, "records.release", record, claim_epoch=1)[2] == "claim_lost"
    fenced = call(
        items, "records.update", record, expected_version=3, claim_epoch=1, data={"title": "x"}
    )
    assert fenced[2] == "claim_lost"
    assert row(items, record["record_id"])["version"] == 3
    # A new holder proceeds with a new epoch.
    _, output, _ = claim(items, record, agent="a2")
    assert output["claim"]["claim_epoch"] == 3


def test_holder_writes_release_and_reclaim(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    held = call(
        items, "records.update", record, expected_version=2, claim_epoch=1, data={"title": "mine"}
    )
    assert held[0] == "succeeded"
    assert call(items, "records.release", record, agent="a2", claim_epoch=1)[2] == "claim_lost"
    _, released, _ = call(items, "records.release", record, claim_epoch=1)
    assert released["record"]["holder"] is None
    assert [e[0] for e in events(items, record["record_id"])] == [
        "created",
        "claimed",
        "updated",
        "released",
    ]
    _, again, _ = claim(items, record, agent="a2")
    assert again["claim"]["claim_epoch"] == 2


def test_an_unswept_lapsed_claim_can_be_taken(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    with items.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE records SET lease_expires_at=? WHERE record_id=?",
            (claims.stamp(now() - timedelta(seconds=1)), record["record_id"]),
        )
    _, output, _ = claim(items, record, agent="a2")
    assert output["claim"]["claim_epoch"] == 3
    kinds = [e[0] for e in events(items, record["record_id"])]
    assert kinds[-2:] == ["claim_expired", "claimed"]


# Renewal only on evidence the holder is alive


def test_renewal_by_reclaim_attempt_start_and_success(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    first = lease(items, record["record_id"])
    _, again, _ = claim(items, record)
    assert again["claim"]["claim_epoch"] == 1 and lease(items, record["record_id"]) >= first
    assert [e[0] for e in events(items, record["record_id"])] == ["created", "claimed"]
    with items.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE records SET lease_expires_at=? WHERE record_id=?",
            (claims.stamp(now() + timedelta(seconds=30)), record["record_id"]),
        )
        deadline = now() + timedelta(seconds=600)
        assert claims.renew_for_attempt(conn, "a1", deadline) == 1
        assert claims.renew_for_attempt(conn, "a2", deadline) == 0
    assert lease(items, record["record_id"]) == deadline + timedelta(seconds=120)
    with items.database.engine.begin() as conn:
        at = now()
        claims.renew_for_success(conn, "a1", at)
    assert lease(items, record["record_id"]) == at + timedelta(seconds=3600)
    assert row(items, record["record_id"])["claim_epoch"] == 1
