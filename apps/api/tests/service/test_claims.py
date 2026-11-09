"""Record claims (ADR-034 slice 2): atomic claims, fencing, renewal, expiry, follow-up
wakes and the apply-time lease bound; portable fakes only."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import test_records as records_test
from test_records import Auth, FakeClient, collection, created, events, put, run

from cardea.service import claims, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.auth import Authentication
from cardea.service.database import Database
from cardea.service.kernel import Kernel, provision
from cardea.service.models import OperationTaskRequest, now
from cardea.service.operation_runtime import OperationRuntime
from cardea.service.operations import Registry
from cardea.service.records import stamp
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


def call(
    kernel: Kernel, operation: str, record: dict[str, Any], agent: str = "a1", **args: Any
) -> Any:
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
    assert listed is not None
    assert [c["record_id"] for c in listed["claims"]] == [record["record_id"]]
    assert listed["claims"][0]["record"]["holder"] == "a1"
    _, others, _ = run(
        items, "records.claimed", {"collection": "items"}, agent="a2", grants=CLAIM_GRANTS
    )
    assert others is not None and others["claims"] == []


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
            (stamp(now() - timedelta(seconds=1)), record["record_id"]),
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
            (stamp(now() + timedelta(seconds=30)), record["record_id"]),
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


# Group members: follow-up wakes and the apply-time lease bound

MEMBER = "desk.worker.0"


@pytest.fixture
def desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Kernel, Any]]:
    import test_groups as groups_test
    from test_agent_runs import FakeSession

    from cardea.service import agent_run_runtime
    from cardea.service.harnesses import (
        IMPLICIT_ID,
        Catalog,
        Harnesses,
        ModelEntry,
        implicit_entry,
    )

    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    db = Database(tmp_path / "data")
    db.migrate()
    rights = (*groups_test.SUPERVISOR, "record:items/claim", "record:items/read")
    provision(db, groups_test.document(rights), groups_test.REGISTRY)
    session = FakeSession()
    entry = implicit_entry("model-a").model_copy(
        update={"models": {"model-a": ModelEntry(), "model-b": ModelEntry()}}
    )
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(
        db, cast(Authentication, Auth()), operations=groups_test.REGISTRY, harnesses=harnesses
    )
    try:
        yield instance, session
    finally:
        db.close()


def apply_items(kernel: Kernel, **claims_config: Any) -> None:
    from cardea.service.records import CollectionDocument, apply_collection

    document = CollectionDocument.model_validate(
        {
            "metadata": {"name": "items", "namespace": "test"},
            "spec": {
                "supervisor": "supervisor",
                "schema": {"type": "object"},
                "claims": {**CLAIMS, **claims_config},
            },
        }
    )
    with kernel.database.engine.begin() as conn:
        apply_collection(conn, "supervisor", document, dry_run=False)


def apply_desk(kernel: Kernel, attempts: int = 1, timeout: int = 60) -> dict[str, Any]:
    import test_groups as groups_test

    body = groups_test.manifest(
        groups_test.pool(
            replicas=1,
            grants=[*groups_test.RUN_GRANTS, "record:items/claim", "record:items/read"],
            run={
                "instructions": "Work on the item you hold.",
                "operations": ["records.claimed", "mail.send"],
                "model": "model-a",
                "bounds": {"timeout_seconds": timeout, "attempts": attempts},
            },
            # Leap days only: without follow-ups the holder would not run again for years.
            triggers=[{"schedule": {"cron": "0 9 29 2 *", "timezone": "UTC", "misfire": "skip"}}],
        )
    )
    body["spec"]["collections"] = ["items"]
    return groups_test.put(kernel, body)


def held_record(kernel: Kernel, expires: datetime) -> str:
    """A record the member holds at epoch 1 (the claim path itself is tested above)."""
    record_id = "rec-" + "a" * 32
    at = now().isoformat()
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO records(namespace,collection,record_id,data_json,holder,claim_epoch,"
            "lease_expires_at,version,created_by,created_at,updated_at) "
            "VALUES ('test','items',?,'{}',?,1,?,2,?,?,?)",
            (record_id, MEMBER, stamp(expires), MEMBER, at, at),
        )
    return record_id


def member_tasks(kernel: Kernel) -> list[str]:
    with kernel.database.read() as conn:
        return [
            str(r[0])
            for r in conn.exec_driver_sql(
                "SELECT request_id FROM task_bindings WHERE agent_id=? ORDER BY rowid", (MEMBER,)
            )
        ]


def test_an_idle_holder_keeps_its_claim_until_follow_ups_run_out(
    desk: tuple[Kernel, Any],
) -> None:
    from cardea.runtime.agent_session import SessionResult
    from cardea.service.agent_run_runtime import AgentRunRuntime

    kernel, session = desk
    apply_items(kernel)
    assert apply_desk(kernel)["change"] == "created"
    record_id = held_record(kernel, now() + timedelta(seconds=3600))

    async def idle(client: Any, request: Any) -> SessionResult:
        return SessionResult(text="Still working.", is_error=False, cost_usd=0.0, turns=1)

    def tick(offset: int) -> None:
        clock = now() + timedelta(seconds=offset)
        Scheduler(kernel, lambda: clock).tick()

    tick(600)  # more than half the lease left: no wake
    assert member_tasks(kernel) == []
    for n in (1, 2):
        tick(1900)  # under half left and the member is idle: one coalesced follow-up
        tick(1900)
        requests = member_tasks(kernel)
        assert requests[-1] == f"{MEMBER}.claim.{record_id}.1.{n}" and len(requests) == n
        with kernel.database.read() as conn:
            task_id = conn.exec_driver_sql(
                "SELECT task_id FROM task_bindings WHERE request_id=?", (requests[-1],)
            ).scalar_one()
        session.scripts = [idle]
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
        # The successful run renewed the full lease; same holder, same epoch.
        stored = row(kernel, record_id)
        assert stored["holder"] == MEMBER and stored["claim_epoch"] == 1
        assert lease(kernel, record_id) > now() + timedelta(seconds=3500)
    tick(1900)  # max_followups (2) reached: no further wake
    assert len(member_tasks(kernel)) == 2
    tick(3700)  # the lease runs out: the claim lapses and is escalated
    stored = row(kernel, record_id)
    assert stored["holder"] is None and stored["claim_epoch"] == 2
    lapsed = journal(kernel, "record.claim_expired")[-1]
    assert lapsed["reason"] == "followups_exhausted" and lapsed["holder"] == MEMBER


def test_a_failing_holder_loses_its_claim_within_lease_and_a_tick(
    desk: tuple[Kernel, Any],
) -> None:
    from cardea.runtime.agent_session import SessionResult
    from cardea.service.agent_run_runtime import AgentRunRuntime

    kernel, session = desk
    apply_items(kernel)
    apply_desk(kernel)
    ends = now() + timedelta(seconds=3600)
    record_id = held_record(kernel, ends)
    clock = now() + timedelta(seconds=1900)
    Scheduler(kernel, lambda: clock).tick()
    with kernel.database.read() as conn:
        task_id = conn.exec_driver_sql(
            "SELECT task_id FROM task_bindings WHERE agent_id=?", (MEMBER,)
        ).scalar_one()

    async def broken(client: Any, request: Any) -> SessionResult:
        return SessionResult(text="", is_error=True, cost_usd=0.0, turns=1)

    session.scripts = [broken]
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    assert lease(kernel, record_id) == ends  # a failed run renews nothing
    after = ends + timedelta(seconds=60)
    Scheduler(kernel, lambda: after).tick()
    assert row(kernel, record_id)["holder"] is None


def test_the_lease_bound_is_checked_at_apply(desk: tuple[Kernel, Any]) -> None:
    from cardea.service.database import Fault

    kernel, _ = desk
    apply_items(kernel, lease_seconds=600)
    # 2 x 2 attempts x 900 s + 240 s = 3840 s > 600 s.
    with pytest.raises(Fault) as short:
        apply_desk(kernel, attempts=2, timeout=900)
    assert short.value.code == "claim_lease_too_short"
    assert "3840" in short.value.message and "worker" in short.value.message
    apply_items(kernel, lease_seconds=3840)
    assert apply_desk(kernel, attempts=2, timeout=900)["change"] == "created"
    # Shortening the lease under a group that may claim is refused too.
    with pytest.raises(Fault) as shrink:
        apply_items(kernel, lease_seconds=3600)
    assert shrink.value.code == "claim_lease_too_short"
