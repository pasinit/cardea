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
from fastapi.testclient import TestClient
from test_records import Auth, FakeClient, collection, created, events, put, run

from cardea.service import claims, operation_runtime
from cardea.service import kernel as handlers
from cardea.service.app import create_app
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
    rights = (*records_test.SUPERVISOR, "record:items/claim", "record:items/force_release")
    provision(db, records_test.document(rights), registry)
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
    assert journal(items, "cardea.record.claim_expired")[-1]["data"]["reason"] == "lease_expired"
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


# Supervisor release (force_release)

FORCE_GRANTS = (*CLAIM_GRANTS, "record:items/force_release")


def force(kernel: Kernel, record: dict[str, Any], key: str = "k1", agent: str = "a1") -> Any:
    return run(
        kernel,
        "records.force_release",
        {
            "collection": "items",
            "record_id": record["record_id"],
            "reason": "holder unresponsive",
            "client_key": key,
        },
        agent=agent,
        grants=FORCE_GRANTS,
    )


def test_force_release_by_the_supervisor_frees_the_record_and_fences_the_holder(
    items: Kernel,
) -> None:
    record = created(items)
    claim(items, record)
    state, output, _ = force(items, record)
    assert state == "succeeded" and output["record"]["holder"] is None
    stored = row(items, record["record_id"])
    assert stored["holder"] is None and stored["lease_expires_at"] is None
    assert stored["claim_epoch"] == 2
    # The old holder's later writes are rejected by epoch.
    stale = call(
        items, "records.update", record, expected_version=3, claim_epoch=1, data={"title": "x"}
    )
    assert stale[2] == "claim_lost"
    assert call(items, "records.release", record, claim_epoch=1)[2] == "claim_lost"
    # Another role claims it at the new epoch.
    _, taken, _ = claim(items, record, agent="a2")
    assert taken["claim"]["claim_epoch"] == 3 and taken["record"]["holder"] == "a2"


def test_force_release_writes_an_audit_entry_with_the_reason(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    force(items, record)
    kind, _, actor, receipt = events(items, record["record_id"])[-1]
    assert (kind, actor) == ("released", "supervisor") and receipt
    with items.database.read() as conn:
        detail = json.loads(
            conn.exec_driver_sql(
                "SELECT detail_json FROM record_events WHERE receipt_id=?", (receipt,)
            ).scalar_one()
        )
    assert detail == {
        "forced": True,
        "reason": "holder unresponsive",
        "previous_holder": "a1",
        "previous_epoch": 1,
    }
    assert detail.items() <= journal(items, "cardea.record.released")[-1]["data"].items()


def test_force_release_refuses_a_caller_who_is_not_the_supervisor(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    # Without the grant, the supervisor is refused.
    without = run(
        items,
        "records.force_release",
        {
            "collection": "items",
            "record_id": record["record_id"],
            "reason": "x",
            "client_key": "k9",
        },
        agent="a2",
        grants=CLAIM_GRANTS,
    )
    assert without[2] == "task_grant_denied"
    # Even holding the grant, a task submitted by someone who is not the collection's
    # supervisor is refused.
    with items.database.engine.begin() as conn:
        spec = json.loads(conn.exec_driver_sql("SELECT spec_json FROM collections").scalar_one())
        spec["supervisor"] = "outsider"
        conn.exec_driver_sql(
            "UPDATE collections SET supervisor_id='outsider', spec_json=?", (json.dumps(spec),)
        )
    assert force(items, record, agent="a2")[2] == "collection_supervisor"
    stored = row(items, record["record_id"])
    assert stored["holder"] == "a1" and stored["claim_epoch"] == 1


def test_force_release_checks_the_supervisor_authority_live(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    with items.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "DELETE FROM policies WHERE principal_id='supervisor' AND operation='groups.manage'"
        )
    assert force(items, record)[0] == "failed"
    assert row(items, record["record_id"])["holder"] == "a1"


def test_a_double_force_release_is_a_no_op(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    first = force(items, record)
    again = force(items, record)
    assert again[0] == "succeeded" and again[1] == first[1]
    # A new key on a record nobody holds changes nothing either.
    assert force(items, record, key="k2")[0] == "succeeded"
    assert row(items, record["record_id"])["claim_epoch"] == 2
    kinds = [e[0] for e in events(items, record["record_id"])]
    assert kinds == ["created", "claimed", "released"]


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


@pytest.mark.loopback
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
    lapsed = journal(kernel, "cardea.record.claim_expired")[-1]["data"]
    assert lapsed["reason"] == "followups_exhausted" and lapsed["previous_holder"] == MEMBER
    # The supervisor is told by mail, committed with the expiry.
    with kernel.database.read() as conn:
        mail = conn.exec_driver_sql(
            "SELECT sender_id,to_kind,to_address,record_json,parts_json FROM mailbox_messages"
        ).all()
    assert len(mail) == 1
    sender, kind, address, record, parts = mail[0]
    assert (sender, kind, address) == ("runtime", "instance", "supervisor")
    assert json.loads(record) == {"collection": "items", "record_id": record_id}
    assert json.loads(parts)[1]["data"]["reason"] == "followups_exhausted"


@pytest.mark.loopback
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


def test_writes_to_a_held_record_are_fenced_by_default(items: Kernel) -> None:
    record = created(items)
    claim(items, record)

    def write(agent: str, version: int, **epoch: Any) -> Any:
        return call(
            items,
            "records.update",
            record,
            agent,
            expected_version=version,
            data={"title": "one"},
            **epoch,
        )

    assert write("a2", 2)[2] == "record_claimed"  # a non-holder
    assert write("a2", 2, claim_epoch=1)[2] == "claim_lost"
    assert write("a1", 2)[2] == "claim_lost"  # the holder must present its epoch
    assert write("a1", 2, claim_epoch=1)[0] == "succeeded"
    # The claim lapses and a2 takes the record: a1 is now a stale holder.
    ends = lease(items, record["record_id"])
    Scheduler(items, lambda: ends + timedelta(seconds=60)).tick()
    claim(items, record, agent="a2")
    assert write("a1", 5)[2] == "record_claimed"
    assert write("a1", 5, claim_epoch=1)[2] == "claim_lost"
    assert write("a2", 5)[2] == "claim_lost"
    assert write("a2", 5, claim_epoch=3)[0] == "succeeded"


def test_a_stale_holder_of_an_unswept_claim_is_refused(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    with items.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE records SET lease_expires_at=? WHERE record_id=?",
            (stamp(now() - timedelta(seconds=1)), record["record_id"]),
        )
    stale = call(items, "records.update", record, expected_version=2, data={"title": "one"})
    assert stale[2] == "claim_lost"
    # Anyone else may write: a lapsed claim no longer holds the record.
    other = call(items, "records.update", record, "a2", expected_version=2, data={"title": "two"})
    assert other[0] == "succeeded"


def test_a_collection_may_opt_out_of_write_fencing(items: Kernel) -> None:
    put(items, collection(claims={**CLAIMS, "fence_writes": False}))
    record = created(items)
    claim(items, record)
    other = call(items, "records.update", record, "a2", expected_version=2, data={"title": "one"})
    assert other[0] == "succeeded"
    # An explicit epoch is still checked.
    stale = call(
        items,
        "records.update",
        record,
        "a2",
        expected_version=3,
        claim_epoch=1,
        data={"title": "two"},
    )
    assert stale[2] == "claim_lost"


def test_the_fencing_default_keeps_the_digest(kernel: Kernel) -> None:
    first = put(kernel, collection(claims=CLAIMS)).json()
    explicit = put(kernel, collection(claims={**CLAIMS, "fence_writes": True})).json()
    assert explicit["plan"]["change"] == "unchanged"
    assert "fence_writes" not in first["spec"]["claims"]
    changed = put(kernel, collection(claims={**CLAIMS, "fence_writes": False})).json()
    assert changed["plan"]["change"] == "updated"
    assert changed["spec"]["claims"]["fence_writes"] is False


@pytest.mark.loopback
def test_an_event_only_holder_keeps_its_claim_through_follow_ups(
    desk: tuple[Kernel, Any],
) -> None:
    """Follow-ups use the member's first trigger of either kind (ADR-034 slice 7)."""
    import test_groups as groups_test

    from cardea.runtime.agent_session import SessionResult
    from cardea.service.agent_run_runtime import AgentRunRuntime

    kernel, session = desk
    apply_items(kernel)
    body = groups_test.manifest(
        groups_test.pool(
            replicas=1,
            grants=[*groups_test.RUN_GRANTS, "record:items/claim", "record:items/read"],
            run={
                "instructions": "Work on the item you hold.",
                "operations": ["records.claimed", "mail.send"],
                "model": "model-a",
                "bounds": {"timeout_seconds": 60, "attempts": 1},
            },
            triggers=[{"event": {"types": ["cardea.mailbox.sent"], "subjects": ["$member"]}}],
        )
    )
    body["spec"]["collections"] = ["items"]
    assert groups_test.put(kernel, body)["change"] == "created"
    record_id = held_record(kernel, now() + timedelta(seconds=3600))

    async def idle(client: Any, request: Any) -> SessionResult:
        return SessionResult(text="Still working.", is_error=False, cost_usd=0.0, turns=1)

    clock = now() + timedelta(seconds=1900)
    Scheduler(kernel, lambda: clock).tick()
    (request,) = member_tasks(kernel)
    assert request == f"{MEMBER}.claim.{record_id}.1.1"
    with kernel.database.read() as conn:
        task_id = conn.exec_driver_sql(
            "SELECT task_id FROM task_bindings WHERE request_id=?", (request,)
        ).scalar_one()
    session.scripts = [idle]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    stored = row(kernel, record_id)
    assert stored["holder"] == MEMBER and stored["claim_epoch"] == 1
    assert lease(kernel, record_id) > now() + timedelta(seconds=3500)


# Only progress renews a claim (ADR-034 2.4, amended 2026-11-17)


def renewal_allowed(kernel: Kernel, record_id: str) -> bool:
    return bool(row(kernel, record_id)["attempt_renewal"])


def test_an_attempt_after_a_failed_one_does_not_extend_the_claim(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    record_id = record["record_id"]
    with items.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE records SET lease_expires_at=? WHERE record_id=?",
            (stamp(now() + timedelta(seconds=30)), record_id),
        )
        deadline = now() + timedelta(seconds=600)
        # The first attempt after the claim (a successful call) renews it.
        assert claims.renew_for_attempt(conn, "a1", deadline) == 1
    renewed = lease(items, record_id)
    assert renewed == deadline + timedelta(seconds=120) and not renewal_allowed(items, record_id)
    with items.database.engine.begin() as conn:
        # That attempt failed (nothing marked progress): the next attempt is not renewed.
        assert claims.renew_for_attempt(conn, "a1", deadline + timedelta(seconds=900)) == 0
    assert lease(items, record_id) == renewed
    # Progress restores it: a successful run, a successful parked session, a re-claim.
    for progress in ("success", "parked", "reclaim"):
        with items.database.engine.begin() as conn:
            claims.renew_for_attempt(conn, "a1", deadline)  # an attempt starts and fails
        assert not renewal_allowed(items, record_id)
        if progress == "reclaim":
            claim(items, record)
        else:
            with items.database.engine.begin() as conn:
                if progress == "success":
                    claims.renew_for_success(conn, "a1", now())
                else:
                    claims.mark_progress(conn, "a1", now())
        assert renewal_allowed(items, record_id), progress


@pytest.mark.loopback
def test_a_holder_whose_attempts_all_fail_loses_the_claim_within_the_bound(
    desk: tuple[Kernel, Any],
) -> None:
    """The ADR-034 bound, amended: after its last progress, a holder whose attempts all fail
    loses the claim within its lease, plus one attempt timeout and the 120 s margin, plus a
    minute tick, and no attempt after a failed one extends it (on a fake scheduler clock)."""
    from cardea.runtime.agent_session import SessionResult
    from cardea.service import agent_run_runtime
    from cardea.service.agent_run_runtime import AgentRunRuntime

    kernel, session = desk
    apply_items(kernel)
    timeout = 60
    apply_desk(kernel, attempts=2, timeout=timeout)
    # The holder's last progress left 30 s of its lease.
    ends = now() + timedelta(seconds=30)
    record_id = held_record(kernel, ends)

    async def broken(client: Any, request: Any) -> SessionResult:
        return SessionResult(text="", is_error=True, cost_usd=0.0, turns=1)

    leases: list[datetime] = []
    for _ in range(2):
        # Under half the lease left and the member idle: a follow-up run, two attempts.
        Scheduler(kernel, now).tick()
        with kernel.database.read() as conn:
            task_id = conn.exec_driver_sql(
                "SELECT task_id FROM task_bindings WHERE agent_id=? ORDER BY rowid DESC LIMIT 1",
                (MEMBER,),
            ).scalar_one()
        session.scripts = [broken, broken]
        assert AgentRunRuntime(kernel).execute(task_id) == "failed"
        leases.append(lease(kernel, record_id))
    # Only the first attempt after the progress extended the claim; the three after it, each
    # following a failed attempt, did not.
    assert leases[0] == leases[1] and leases[0] > ends
    # The bound: the attempt's lease (its timeout plus the settle margin) plus 120 s.
    with kernel.database.read() as conn:
        request = json.loads(
            conn.exec_driver_sql(
                "SELECT request_json FROM task_bindings WHERE task_id=?", (task_id,)
            ).scalar_one()
        )
    attempt = request["attempt_timeout_seconds"] + agent_run_runtime.LEASE_MARGIN
    assert leases[0] <= ends + timedelta(seconds=attempt + 120)
    # The sweep releases it within one tick of that expiry.
    Scheduler(kernel, lambda: leases[0] - timedelta(seconds=1)).tick()
    assert row(kernel, record_id)["holder"] == MEMBER
    Scheduler(kernel, lambda: leases[0] + timedelta(seconds=60)).tick()
    stored = row(kernel, record_id)
    assert stored["holder"] is None and stored["claim_epoch"] == 2


# Supervisor release over HTTP

def release_http(
    kernel: Kernel, record: dict[str, Any], token: str = "supervisor", reason: str = "stuck"
) -> Any:
    return TestClient(create_app(kernel)).post(
        f"/v0.2/collections/items/records/{record['record_id']}/release",
        params={"namespace": "test"},
        json={"reason": reason},
        headers={**records_test.HEADERS, "Authorization": f"Bearer {token}"},
    )


def test_the_supervisor_releases_a_claim_over_http_with_history(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    done = release_http(items, record)
    assert done.status_code == 200, done.json()
    assert done.json()["released"] is True
    stored = row(items, record["record_id"])
    assert stored["holder"] is None and stored["claim_epoch"] == 2
    last = events(items, record["record_id"])[-1]
    assert last[0] == "released" and last[2] == "supervisor"
    # The old holder is fenced; a repeat changes nothing.
    assert call(items, "records.release", record, claim_epoch=1)[2] == "claim_lost"
    again = release_http(items, record)
    assert again.status_code == 200 and again.json()["released"] is False
    assert row(items, record["record_id"])["claim_epoch"] == 2


def test_http_release_is_refused_for_anyone_but_the_supervisor(items: Kernel) -> None:
    record = created(items)
    claim(items, record)
    refused = release_http(items, record, token="outsider")
    assert refused.status_code in {403, 404}
    assert row(items, record["record_id"])["holder"] is not None
