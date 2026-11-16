# ruff: noqa: F811 - the `kernel` and `session` fixtures are shared with test_groups
"""Draining and deleting agent groups and collections (ADR-034 decision 1.5, slice 8);
portable fakes only. Fixtures come from the agent-group tests."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_groups import (  # noqa: F401 - pytest fixtures
    RUN_GRANTS,
    done,
    kernel,
    manifest,
    pool,
    put,
    rows,
    session,
)

from cardea.service import mailbox
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.database import Fault, borrow
from cardea.service.groups import reconcile_all
from cardea.service.kernel import Kernel
from cardea.service.models import RIGHTS, AgentRunRequest, now
from cardea.service.records import CollectionDocument, apply_collection, check_may_claim
from cardea.service.triggers import Scheduler

HEADERS = {"Authorization": "Bearer supervisor"}


def members(kernel: Kernel) -> dict[str, tuple[str, int]]:
    """Each member's state and whether its principal is disabled."""
    return {
        str(r[0]): (str(r[1]), int(r[2]))
        for r in rows(
            kernel,
            "SELECT m.principal_id,m.state,p.disabled FROM group_members m "
            "JOIN principals p USING(principal_id) ORDER BY m.principal_id",
        )
    }


def live_triggers(kernel: Kernel) -> list[str]:
    return [
        str(r[0])
        for r in rows(
            kernel,
            "SELECT trigger_id FROM triggers WHERE managed_by='group' AND removed=0 "
            "ORDER BY trigger_id",
        )
    ]


def generations(kernel: Kernel, name: str = "desk") -> tuple[int, int, str]:
    ((generation, observed, state),) = rows(
        kernel,
        "SELECT generation,observed_generation,state FROM agent_groups WHERE name=?",
        name,
    )
    return int(generation), int(observed), str(state)


def run_for(kernel: Kernel, member: str, key: str = "manual") -> str:
    """A member's run, admitted and left unfinished (queued) as a slow run would be."""
    request = AgentRunRequest.model_validate(
        {
            "request_id": f"{member}.{key}",
            "agent_id": member,
            "namespace": "test",
            "objective": "Manual run",
            "instructions": "Do it.",
            "operations": ["listings.search"],
            "grants": [*RIGHTS, "models.invoke", "listings.search"],
            "model": "model-a",
        }
    )
    return kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id


def task_state(kernel: Kernel, task_id: str) -> str:
    with kernel.database.read() as conn, borrow(conn) as tx:
        return tx.get_task(task_id).state.value


# Scale-down, pool removal, rename


def test_scale_down_drains_idle_members_highest_ordinal_first(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=3)))
    shrunk = put(kernel, manifest(pool(replicas=1)))
    assert shrunk["plan"]["members_draining"] == ["desk.worker.2", "desk.worker.1"]
    assert shrunk["plan"]["members_removed"] == ["desk.worker.2", "desk.worker.1"]
    assert members(kernel) == {
        "desk.worker.0": ("active", 0),
        "desk.worker.1": ("removed", 1),
        "desk.worker.2": ("removed", 1),
    }
    assert live_triggers(kernel) == ["desk.worker.0.t0"]
    # Nothing drains, so the reconciler observed the new generation in the apply.
    assert generations(kernel) == (2, 2, "active")
    assert shrunk["group"]["status"]["observedGeneration"] == 2
    # Scaling back up reactivates the same principal.
    grown = put(kernel, manifest(pool(replicas=2)))
    assert grown["plan"]["members_added"] == ["desk.worker.1"]
    assert members(kernel)["desk.worker.1"] == ("active", 0)
    assert live_triggers(kernel) == ["desk.worker.0.t0", "desk.worker.1.t0"]


def test_a_busy_member_finishes_its_run_until_the_drain_timeout(
    kernel: Kernel, session: Any
) -> None:
    put(kernel, manifest(pool(replicas=2, drain_timeout_seconds=600)))
    task_id = run_for(kernel, "desk.worker.1")
    shrunk = put(kernel, manifest(pool(replicas=1, drain_timeout_seconds=600)))
    assert shrunk["plan"]["members_draining"] == ["desk.worker.1"]
    assert shrunk["plan"]["members_removed"] == []
    assert members(kernel)["desk.worker.1"] == ("draining", 0)
    assert live_triggers(kernel) == ["desk.worker.0.t0"]  # no new slots
    assert generations(kernel) == (2, 1, "active")
    # No new runs for a draining member.
    with pytest.raises(Fault) as refused:
        run_for(kernel, "desk.worker.1", "second")
    assert refused.value.code == "member_binding"
    # Before the deadline the run is left alone; after it, cancelled through the normal path.
    reconcile_all(kernel, now() + timedelta(seconds=60))
    assert rows(kernel, "SELECT COUNT(*) FROM control_requests WHERE task_id=?", task_id) == [(0,)]
    reconcile_all(kernel, now() + timedelta(seconds=700))
    assert rows(
        kernel, "SELECT kind,request_id FROM control_requests WHERE task_id=?", task_id
    ) == [("cancel", "drain.desk.worker.1.2")]
    assert members(kernel)["desk.worker.1"] == ("draining", 0)
    session.scripts = [done]
    AgentRunRuntime(kernel).execute(task_id)
    assert task_state(kernel, task_id) in {"cancelled", "failed"}
    # The next tick removes the member and observes the generation.
    at = datetime.now(UTC) + timedelta(seconds=720)
    Scheduler(kernel, lambda: at).tick()
    assert members(kernel)["desk.worker.1"] == ("removed", 1)
    assert generations(kernel) == (2, 2, "active")


def test_a_renamed_pool_is_a_removal_and_an_addition(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=1)))
    renamed = put(kernel, manifest(pool(name="crew", replicas=1)))
    assert renamed["plan"]["members_added"] == ["desk.crew.0"]
    assert renamed["plan"]["members_draining"] == ["desk.worker.0"]
    assert members(kernel) == {"desk.crew.0": ("active", 0), "desk.worker.0": ("removed", 1)}
    assert live_triggers(kernel) == ["desk.crew.0.t0"]


def test_a_dry_run_drain_changes_nothing(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=2)))
    before = (members(kernel), live_triggers(kernel), generations(kernel))
    planned = put(kernel, manifest(pool(replicas=1)), dry_run=True)
    assert planned["plan"]["members_draining"] == ["desk.worker.1"]
    assert (members(kernel), live_triggers(kernel), generations(kernel)) == before


# Claims and mail of a removed member


def items(kernel: Kernel, supervisor: str = "supervisor") -> None:
    document = CollectionDocument.model_validate(
        {
            "metadata": {"name": "items", "namespace": "test"},
            "spec": {
                "supervisor": supervisor,
                "schema": {"type": "object"},
                "claims": {"lease_seconds": 3600, "standalone": True},
            },
        }
    )
    with kernel.database.engine.begin() as conn:
        apply_collection(conn, supervisor, document, dry_run=False)


def held_by(kernel: Kernel, member: str, record_id: str = "rec-" + "b" * 32) -> str:
    at = now()
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO records(namespace,collection,record_id,data_json,holder,claim_epoch,"
            "lease_expires_at,version,created_by,created_at,updated_at) "
            "VALUES ('test','items',?,'{}',?,1,?,2,?,?,?)",
            (
                record_id,
                member,
                (at + timedelta(hours=1)).isoformat(timespec="microseconds"),
                member,
                at.isoformat(),
                at.isoformat(),
            ),
        )
    return record_id


def mail_to(kernel: Kernel, member: str) -> None:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        mailbox.send(
            conn,
            tx,
            namespace="test",
            sender_id="supervisor",
            supervisor_id="supervisor",
            to=mailbox.Address(instance=member),
            parts=[{"text": "please finish the review"}],
            runtime_id=kernel.runtime_id,
            at=now(),
        )


def deliveries(kernel: Kernel) -> list[tuple[Any, ...]]:
    return rows(
        kernel,
        "SELECT d.recipient_id,d.pool,d.state,m.kind FROM mailbox_deliveries d "
        "JOIN mailbox_messages m USING(message_id) ORDER BY d.delivery_id",
    )


def test_a_removed_member_releases_claims_and_its_mail_goes_to_the_pool(
    kernel: Kernel,
) -> None:
    items(kernel)
    put(kernel, manifest(pool(replicas=2)))
    record_id = held_by(kernel, "desk.worker.1")
    mail_to(kernel, "desk.worker.1")
    put(kernel, manifest(pool(replicas=1)))
    ((holder, epoch, version),) = rows(
        kernel, "SELECT holder,claim_epoch,version FROM records WHERE record_id=?", record_id
    )
    assert (holder, epoch, version) == (None, 2, 3)
    ((payload,),) = rows(
        kernel, "SELECT payload FROM events WHERE event_type='cardea.record.claim_expired'"
    )
    assert json.loads(payload)["data"]["reason"] == "member_removed"
    # Instance mail is re-addressed to the pool, where the remaining member gets it.
    assert deliveries(kernel) == [(None, "desk.worker", "pending", "message")]
    with kernel.database.read() as conn:
        assert mailbox.member_pool(conn, "test", "desk.worker.0") == "desk.worker"


def test_mail_with_no_pool_left_is_dead_lettered_to_the_supervisor(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=1)))
    mail_to(kernel, "desk.worker.0")
    put(kernel, manifest(pool(name="crew", replicas=1)))
    states = deliveries(kernel)
    assert states[0] == ("desk.worker.0", None, "dead", "message")
    # The supervisor is told, by a notice about the dead message.
    assert ("supervisor", None, "pending", "notice") in states


def test_a_draining_member_takes_no_new_claims(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=2, drain_timeout_seconds=600)))
    run_for(kernel, "desk.worker.1")
    put(kernel, manifest(pool(replicas=1, drain_timeout_seconds=600)))
    with kernel.database.read() as conn:
        check_may_claim(conn, "desk.worker.0")
        with pytest.raises(Exception) as refused:
            check_may_claim(conn, "desk.worker.1")
    assert getattr(refused.value, "code", "") == "member_draining"


# Deleting groups


def delete(kernel: Kernel, name: str = "desk", **params: Any) -> Any:
    return TestClient(create_app(kernel)).delete(
        f"/v0.2/groups/{name}", params={"namespace": "test", **params}, headers=HEADERS
    )


def test_deleting_a_group_drains_every_member(kernel: Kernel, session: Any) -> None:
    put(kernel, manifest(pool(replicas=2, drain_timeout_seconds=60)))
    task_id = run_for(kernel, "desk.worker.0")
    planned = delete(kernel, dry_run="true").json()
    assert planned["members_draining"] == ["desk.worker.1", "desk.worker.0"]
    assert generations(kernel) == (1, 1, "active")
    deleted = delete(kernel)
    assert deleted.status_code == 200, deleted.json()
    body = deleted.json()
    assert body["change"] == "deleting" and body["waiting"] == ["desk.worker.0"]
    assert body["members_removed"] == ["desk.worker.1"]
    assert live_triggers(kernel) == []
    # Re-applying while members drain is refused.
    with pytest.raises(Fault) as busy:
        put(kernel, manifest())
    assert busy.value.code == "group_deleting"
    reconcile_all(kernel, now() + timedelta(seconds=120))
    session.scripts = [done]
    AgentRunRuntime(kernel).execute(task_id)
    reconcile_all(kernel, now() + timedelta(seconds=180))
    assert generations(kernel) == (1, 1, "deleted")
    assert set(members(kernel).values()) == {("removed", 1)}
    view = TestClient(create_app(kernel)).get(
        "/v0.2/groups/desk", params={"namespace": "test"}, headers=HEADERS
    )
    assert view.json()["status"]["state"] == "deleted"
    # The name can be applied again; the members come back with the same principals.
    revived = put(kernel, manifest())
    assert revived["generation"] == 2 and generations(kernel) == (2, 2, "active")
    assert members(kernel)["desk.worker.0"] == ("active", 0)


# Deleting collections


def delete_collection(kernel: Kernel, **params: Any) -> Any:
    return TestClient(create_app(kernel)).delete(
        "/v0.2/collections/items", params={"namespace": "test", **params}, headers=HEADERS
    )


def test_a_collection_in_use_cannot_be_deleted(kernel: Kernel) -> None:
    items(kernel)
    body = manifest(pool(replicas=1))
    body["spec"]["collections"] = ["items"]
    put(kernel, body)
    refused = delete_collection(kernel)
    assert refused.status_code == 409 and refused.json()["code"] == "collection_in_use"
    body["spec"]["collections"] = []
    put(kernel, body)
    record_id = held_by(kernel, "desk.worker.0")
    planned = delete_collection(kernel, dry_run="true").json()
    assert planned["claims_released"] == 1
    assert rows(kernel, "SELECT deleted_at FROM collections") == [(None,)]
    deleted = delete_collection(kernel).json()
    assert deleted["change"] == "deleted" and deleted["records"] == 1
    # The records and their history stay; the claim was released.
    assert rows(kernel, "SELECT holder FROM records WHERE record_id=?", record_id) == [(None,)]
    client = TestClient(create_app(kernel))
    gone = client.get("/v0.2/collections/items", params={"namespace": "test"}, headers=HEADERS)
    assert gone.status_code == 404
    # A group cannot use it, and its name cannot be reused.
    body["spec"]["collections"] = ["items"]
    with pytest.raises(Fault) as missing:
        put(kernel, body)
    assert missing.value.code == "collection_not_found"
    with pytest.raises(Fault) as reused:
        items(kernel)
    assert reused.value.code == "collection_deleted"
