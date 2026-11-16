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
    assert rows(kernel, "SELECT COUNT(*) FROM control_requests WHERE task_id=?", task_id) == [
        (0,)
    ]
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
