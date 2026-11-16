"""Group and pool spend and windowed pool expectations (ADR-034 slice 9); portable fakes.

Acceptance test 8 (budgets) plus per-run cost accounting and window-close expectations.
"""

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
import test_groups as groups
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from test_agent_runs import FakeSession, call
from test_groups import manifest, pool, put, rows

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import group_windows, spend
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.database import Fault
from cardea.service.kernel import Kernel
from cardea.service.models import AgentRunRequest

kernel, session = groups.kernel, groups.session
REQUESTS = count()


def spending(group: dict[str, Any] | None = None, **pool_changes: Any) -> dict[str, Any]:
    body = manifest(
        pool(
            replicas=3,
            run={
                "instructions": "Search listings and email one recap.",
                "operations": ["listings.search", "mail.send"],
                "model": "model-a",
                "bounds": {"timeout_seconds": 60, "attempts": 1, "max_cost_usd": 0.5},
            },
            **pool_changes,
        )
    )
    if group is not None:
        body["spec"]["spend"] = group
    return body


def member_request(kernel: Kernel, member: str) -> AgentRunRequest:
    ((definition,),) = rows(
        kernel, "SELECT definition_json FROM triggers WHERE trigger_id=?", f"{member}.t0"
    )
    template = json.loads(definition)["template"]
    return AgentRunRequest.model_validate(
        {**template, "request_id": f"{member}.run-{next(REQUESTS)}"}
    )


def admit(kernel: Kernel, member: str) -> str:
    supervisor = kernel.auth.validate("supervisor")
    return kernel.submit(supervisor, member_request(kernel, member)).task.task_id


def costing(cost: float | None, *, send: bool = False) -> Any:
    async def script(client: ClientSession, request: SessionRequest) -> SessionResult:
        if send:
            sent = await call(client, "mail-send", to="user@example.invalid")
            assert not sent.is_error, sent.content
        return SessionResult(text="Done.", is_error=False, cost_usd=cost)

    return script


# 8. Budgets


def test_group_ceiling_refuses_the_third_concurrent_admission(kernel: Kernel) -> None:
    put(kernel, spending({"usd": 1.0, "window": "day"}))
    admit(kernel, "desk.worker.0")
    admit(kernel, "desk.worker.1")
    with pytest.raises(Fault) as refused:
        admit(kernel, "desk.worker.2")
    assert refused.value.code == "budget_exhausted" and refused.value.status == 429
    assert "group spend ceiling" in refused.value.message
    # The refused admission left nothing behind.
    assert rows(kernel, "SELECT COUNT(*) FROM spend_reservations") == [(2,)]
    assert rows(kernel, "SELECT COUNT(*) FROM task_bindings WHERE agent_id='desk.worker.2'") == [
        (0,)
    ]


def test_unused_reservation_is_released_and_unknown_cost_is_kept(
    kernel: Kernel, session: FakeSession
) -> None:
    put(kernel, spending({"usd": 1.2, "window": "day"}))
    first = admit(kernel, "desk.worker.0")
    admit(kernel, "desk.worker.1")
    with pytest.raises(Fault):
        admit(kernel, "desk.worker.2")  # 0.5 + 0.5 + 0.5 > 1.2
    session.scripts = [costing(0.1)]
    assert AgentRunRuntime(kernel).execute(first) == "succeeded"
    # 0.1 settled + 0.5 reserved + 0.5 = 1.1 <= 1.2: the unused 0.4 was released.
    third = admit(kernel, "desk.worker.2")
    assert rows(kernel, "SELECT state,cost_usd FROM spend_reservations WHERE task_id=?", first) == [
        ("settled", 0.1)
    ]
    session.scripts = [costing(None)]
    assert AgentRunRuntime(kernel).execute(third) == "succeeded"
    view = TestClient(create_app(kernel)).get(
        f"/v0.2/tasks/{third}/run", headers={"Authorization": "Bearer supervisor"}
    )
    assert view.status_code == 200, view.text
    charged = view.json()["spend"]
    assert charged["attempts"] == [{"schema_version": "0.2", "attempt": 1, "cost_usd": None}]
    assert charged["cost_usd"] is None
    reservation = charged["reservation"]
    assert reservation["state"] == "unknown" and reservation["charged_usd"] == 0.5
    with kernel.database.engine.begin() as conn:
        spend.settle(conn, "test", "desk", datetime.now(UTC))
        key = spend.window("day", "UTC", datetime.now(UTC)).key
        assert spend.used(conn, "test", "desk", key=key) == pytest.approx(1.1)


def test_pool_ceiling_is_its_own_account(kernel: Kernel) -> None:
    put(kernel, spending(None, spend={"usd": 0.5, "window": "week", "timezone": "Europe/Rome"}))
    admit(kernel, "desk.worker.0")
    with pytest.raises(Fault) as refused:
        admit(kernel, "desk.worker.1")
    assert refused.value.code == "budget_exhausted" and "pool worker" in refused.value.message
    ((group_window, pool_window),) = rows(
        kernel, "SELECT group_window,pool_window FROM spend_reservations"
    )
    assert group_window is None and pool_window.startswith("week:Europe/Rome:")


def test_runs_outside_groups_record_cost_and_reserve_nothing(
    kernel: Kernel, session: FakeSession
) -> None:
    request = AgentRunRequest.model_validate(
        {
            "request_id": "plain",
            "agent_id": "taken.worker.0",
            "namespace": "test",
            "objective": "o",
            "instructions": "i",
            "operations": ["listings.search"],
            "grants": list(groups.RUN_GRANTS),
            "model": "model-a",
            "attempt_timeout_seconds": 30,
        }
    )
    task_id = kernel.submit(kernel.auth.validate("supervisor"), request).task.task_id
    session.scripts = [costing(0.25)]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert rows(kernel, "SELECT attempt,cost_usd FROM run_attempt_costs") == [(1, 0.25)]
    assert rows(kernel, "SELECT COUNT(*) FROM spend_reservations") == [(0,)]


def test_windows_follow_the_local_calendar() -> None:
    at = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)  # a Sunday, the day London's DST starts
    day = spend.window("day", "Europe/London", at)
    assert day.key == "day:Europe/London:2026-03-29"
    assert day.end - day.start == timedelta(hours=23)  # the short day
    week = spend.window("week", "UTC", at)
    assert week.key == "week:UTC:2026-W13"
    assert week.start == datetime(2026, 3, 23, tzinfo=UTC)  # Monday
    assert spend.previous("day", "UTC", at).key == "day:UTC:2026-03-28"


def test_spend_fields_are_validated(kernel: Kernel) -> None:
    with pytest.raises(ValueError):
        put(kernel, spending({"usd": 1, "window": "day", "timezone": "Mars/Base"}))
    with pytest.raises(ValueError):
        put(kernel, spending(None, expect_window=[{"operation": "mail.send", "window": "day"}]))


# Windowed pool expectations


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Create groups at the start of today (UTC), so today's window is checked."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("cardea.service.groups.now", lambda: start)
    return start


def supervisor_mail(kernel: Kernel) -> list[dict[str, Any]]:
    found = rows(
        kernel,
        "SELECT m.parts_json FROM mailbox_messages m JOIN mailbox_deliveries d "
        "USING(message_id) WHERE d.recipient_id='supervisor' ORDER BY m.created_at",
    )
    return [json.loads(r[0]) for r in found]


def test_window_close_mails_the_supervisor_once(
    kernel: Kernel, session: FakeSession, today: datetime
) -> None:
    expectations = [
        {"operation": "mail.send", "min": 1, "window": "day"},
        {"operation": "mail.send", "min": 2, "window": "day"},
        {"operation": "mail.send", "max": 0, "window": "day"},
    ]
    put(kernel, spending(None, expect_window=expectations))
    task_id = admit(kernel, "desk.worker.0")
    session.scripts = [costing(0.1, send=True)]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    # Nothing is checked before the window closes.
    assert set(group_windows.tick(kernel).values()) == {"skipped"}
    closed = today + timedelta(days=1, seconds=30)
    outcomes = sorted(group_windows.tick(kernel, closed).values())
    assert outcomes == ["expectation_exceeded", "expectation_unmet", "met"]
    mail = supervisor_mail(kernel)
    codes = sorted(parts[1]["data"]["code"] for parts in mail)
    assert codes == ["expectation_exceeded", "expectation_unmet"]
    for parts in mail:
        assert parts[1]["data"]["observed"] == 1 and parts[1]["data"]["pool"] == "worker"
    # A later tick in the same window reports nothing again.
    assert set(group_windows.tick(kernel, closed + timedelta(hours=1)).values()) == {"reported"}
    assert len(supervisor_mail(kernel)) == 2
    # Expectations never fail a run after the fact.
    assert rows(kernel, "SELECT state FROM tasks WHERE task_id=?", task_id) == [("succeeded",)]
