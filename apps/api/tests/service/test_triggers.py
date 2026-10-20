"""Time triggers (ADR-024): slot admission, misfire, DST and authority; portable fakes only."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, ValidationError

from cardea.service import kernel as handlers
from cardea.service import schedule
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
    TriggerSpec,
)
from cardea.service.operations import OperationContext, OperationDescriptor, Registry
from cardea.service.triggers import Scheduler


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.enqueued: list[str] = []

    def enqueue_in_transaction(self, conn: Any, options: Any, *args: Any) -> None:
        self.enqueued.append(options["workflow_id"])

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


class Crash(BaseException):
    """Process death: not an `Exception`, so nothing in the scheduler absorbs it."""


def descriptor(required: tuple[str, ...] = ("to", "body")) -> dict[str, Any]:
    return {
        "tool": {
            "name": "mail.send",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "body": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": list(required),
                "additionalProperties": False,
            },
            "outputSchema": {"type": "object"},
        },
        "cardea": {"version": 1, "effect": "effect", "handler": "test.handler"},
    }


async def handler(arguments: dict[str, JsonValue], context: OperationContext) -> Any:
    return {}


def registry(required: tuple[str, ...] = ("to", "body")) -> Registry:
    return Registry(
        (OperationDescriptor.model_validate(descriptor(required)),), {"test.handler": handler}
    )


GRANTS = (*RIGHTS, "mail.send")
LONDON = "Europe/London"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def template(**changes: Any) -> dict[str, JsonValue]:
    return {
        "agent_id": "agent",
        "namespace": "test",
        "objective": "Daily recap",
        "operation": "mail.send",
        "arguments": {"to": "user@example.invalid", "body": "recap"},
        "grants": list(GRANTS),
        **changes,
    }


def spec(**changes: Any) -> TriggerSpec:
    return TriggerSpec.model_validate(
        {
            "trigger_id": "daily",
            "supervisor_id": "supervisor",
            "schedule": "30 8 * * *",
            "timezone": LONDON,
            "misfire": "skip",
            "template_kind": "operation",
            "template": template(),
            **changes,
        }
    )


PRINCIPALS = tuple(
    Principal(
        principal_id=p,
        kind="service" if p == "runtime" else "human",
        auth_kind="spiffe",
        issuer="test",
        subject=p,
    )
    for p in ("runtime", "supervisor", "outsider")
) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)


def document(
    triggers: tuple[TriggerSpec, ...],
    rights: tuple[str, ...] = GRANTS,
    principals: tuple[Principal, ...] = PRINCIPALS,
) -> Provisioning:
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            # Policies expire against the real clock, not the simulated trigger clock.
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        for right in rights
    )
    return Provisioning(
        principals=principals,
        permissions=granted,
        platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        triggers=triggers,
    )


class Clock:
    def __init__(self) -> None:
        self.now = at("2026-10-01T06:00:00")

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    # Installation anchors a trigger at provisioning time.
    monkeypatch.setattr(handlers, "now", value)
    return value


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(()), registry())
    instance = Kernel(db, cast(Authentication, Auth()), operations=registry())
    try:
        yield instance
    finally:
        db.close()


def install(kernel: Kernel, *triggers: TriggerSpec, rights: tuple[str, ...] = GRANTS) -> None:
    provision(kernel.database, document(triggers, rights), kernel.operations)


def tick(kernel: Kernel, clock: Clock, when: str) -> dict[str, str]:
    clock.now = at(when)
    return Scheduler(kernel, clock).tick()


def tasks(kernel: Kernel) -> list[str]:
    with kernel.database.read() as conn:
        return [
            str(r[0])
            for r in conn.exec_driver_sql("SELECT request_id FROM task_bindings ORDER BY rowid")
        ]


def events(kernel: Kernel, trigger_id: str = "daily") -> list[dict[str, Any]]:
    with kernel.database.read() as conn:
        return [
            {**dict(r), "detail": json.loads(r["detail_json"])}
            for r in conn.exec_driver_sql(
                "SELECT kind,slot,task_id,code,detail_json FROM trigger_events "
                "WHERE trigger_id=? ORDER BY event_id",
                (trigger_id,),
            ).mappings()
        ]


def kinds(kernel: Kernel) -> list[str]:
    return [e["kind"] for e in events(kernel)]


# Pure slot arithmetic


def slots(expression: str, start: str, count: int) -> list[str]:
    generator = schedule.forward(expression, LONDON, at(start))
    return [schedule.slot_key(next(generator)) for _ in range(count)]


def test_london_daily_slots_follow_dst() -> None:
    assert slots("30 8 * * *", "2026-03-27T12:00:00", 4) == [
        "20260328T083000Z",
        "20260329T073000Z",  # BST starts on the last Sunday of March.
        "20260330T073000Z",
        "20260331T073000Z",
    ]
    assert slots("30 8 * * *", "2026-10-23T12:00:00", 4) == [
        "20261024T073000Z",
        "20261025T083000Z",  # GMT resumes on the last Sunday of October.
        "20261026T083000Z",
        "20261027T083000Z",
    ]


def test_nonexistent_and_repeated_local_times_run_once() -> None:
    # 01:30 does not exist on 2026-03-29: it runs when the gap ends (02:00 BST).
    assert slots("30 1 * * *", "2026-03-27T12:00:00", 3) == [
        "20260328T013000Z",
        "20260329T010000Z",
        "20260330T003000Z",
    ]
    # 01:30 occurs twice on 2026-10-25: it runs once, at the first occurrence.
    assert slots("30 1 * * *", "2026-10-23T12:00:00", 3) == [
        "20261024T003000Z",
        "20261025T003000Z",
        "20261026T013000Z",
    ]
    # Two local times inside the gap collapse to one slot.
    assert slots("15,45 1 * * *", "2026-03-28T12:00:00", 2) == [
        "20260329T010000Z",
        "20260330T001500Z",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"schedule": "0 30 8 * * *"},  # seconds field
        {"schedule": "H 8 * * *"},  # croniter hash extension
        {"schedule": "30 8 L * *"},
        {"schedule": "0 0 30 2 *"},  # never fires
        {"timezone": "Mars/Olympus"},
        {"misfire": "grace"},
        {"misfire": "skip", "grace_seconds": 600},
        {"misfire": "grace", "grace_seconds": 10},
        {"template": {**template(), "request_id": "fixed"}},
        {"template": {**template(), "idempotency_context": "fixed"}},
        {"template": {**template(), "slot_time": "2026-10-01T08:30:00+01:00"}},
        {"template": template(arguments="not-an-object")},
        {"template": template(grants=list(RIGHTS))},
        {"trigger_id": "x" * 112},
    ],
)
def test_invalid_trigger_definitions_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(**change)


def test_longest_trigger_id_still_forms_a_valid_request_id() -> None:
    long = spec(trigger_id="x" * 111)
    request = long.request(f"{long.trigger_id}.{schedule.slot_key(at('2026-10-01T00:00:00'))}")
    assert isinstance(request, OperationTaskRequest) and len(request.request_id) == 128
    # Effects within one slot share the slot identity as their idempotency context.
    assert request.idempotency_context == request.request_id
    assert request.slot_time is None
    timed = long.request(request.request_id, at("2026-10-01T07:30:00"))
    assert isinstance(timed, OperationTaskRequest) and timed.slot_time is not None
    # The slot is recorded on the trigger's wall clock (London summer time here).
    assert timed.slot_time.isoformat() == "2026-10-01T08:30:00+01:00"


# Installation


def test_installation_requires_installed_operations_and_known_supervisor(
    kernel: Kernel, clock: Clock
) -> None:
    with pytest.raises(Fault) as unknown:
        install(kernel, spec(supervisor_id="nobody"))
    assert unknown.value.code == "unknown_principal"
    kernel.operations = Registry((), {})
    with pytest.raises(Fault) as missing:
        provision(kernel.database, document((spec(),), RIGHTS), kernel.operations)
    assert missing.value.code == "operation_not_installed"
    kernel.operations = registry()
    install(kernel, spec())
    with pytest.raises(Fault) as reassigned:
        install(kernel, spec(supervisor_id="outsider"))
    assert reassigned.value.code == "trigger_immutable"
    assert kinds(kernel) == ["installed"]


# Admission


def test_one_admission_per_slot_under_repeated_ticks(kernel: Kernel, clock: Clock) -> None:
    install(kernel, spec())
    assert tick(kernel, clock, "2026-10-01T07:29:59") == {"daily": "idle"}
    assert tick(kernel, clock, "2026-10-01T07:30:04") == {"daily": "admitted"}
    for when in ("2026-10-01T07:30:05", "2026-10-01T07:31:00", "2026-10-01T23:00:00"):
        assert tick(kernel, clock, when) == {"daily": "idle"}
    assert tick(kernel, clock, "2026-10-02T07:30:01") == {"daily": "admitted"}
    assert tasks(kernel) == ["daily.20261001T073000Z", "daily.20261002T073000Z"]
    admitted = [e for e in events(kernel) if e["kind"] == "admitted"]
    assert [e["detail"]["late"] for e in admitted] == [False, False]
    assert len(cast(Any, kernel.client).enqueued) == 2
    # The admitted task is bound to the trigger's supervisor and its live grants.
    view = kernel.inspect(kernel.auth.validate("supervisor"), admitted[0]["task_id"])
    assert view.supervisor_id == "supervisor" and view.runtime_principal_id == "runtime"


@pytest.mark.parametrize(
    "stage", ["trigger.slot_computed", "admission.before_commit", "trigger.before_commit"]
)
def test_crash_before_commit_then_retick_admits_once(
    kernel: Kernel, clock: Clock, stage: str
) -> None:
    install(kernel, spec())

    def crash(name: str) -> None:
        if name == stage:
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        tick(kernel, clock, "2026-10-01T07:30:02")
    assert tasks(kernel) == [] and kinds(kernel) == ["installed"]
    kernel.fault = lambda name: None
    assert tick(kernel, clock, "2026-10-01T07:30:40") == {"daily": "admitted"}
    assert tick(kernel, clock, "2026-10-01T07:31:00") == {"daily": "idle"}
    assert tasks(kernel) == ["daily.20261001T073000Z"]


def test_crash_after_commit_then_retick_does_not_duplicate(kernel: Kernel, clock: Clock) -> None:
    install(kernel, spec())

    def crash(name: str) -> None:
        if name == "trigger.after_commit":
            raise Crash

    kernel.fault = crash
    with pytest.raises(Crash):
        tick(kernel, clock, "2026-10-01T07:30:02")
    kernel.fault = lambda name: None
    assert tick(kernel, clock, "2026-10-01T07:30:40") == {"daily": "idle"}
    assert tasks(kernel) == ["daily.20261001T073000Z"]


def test_repeated_slot_admission_returns_the_same_task(kernel: Kernel, clock: Clock) -> None:
    """Slot request IDs make admission idempotent even if slot state were lost."""
    install(kernel, spec())
    request = spec().request("daily.20261001T073000Z")
    ids = []
    for _ in range(2):
        with kernel.database.engine.begin() as conn, borrow(conn) as tx:
            ids.append(kernel.admit_scheduled(conn, tx, "supervisor", request, {}).task.task_id)
    assert ids[0] == ids[1] and len(tasks(kernel)) == 1


# Downtime and misfire policy: installed 2026-10-01 06:00Z, first tick 2026-10-04 07:45Z,
# 15 minutes after the fourth daily slot.


@pytest.mark.parametrize(
    ("misfire", "grace", "admitted", "missed"),
    [
        ("skip", None, [], 4),
        ("run_once_late", None, ["daily.20261004T073000Z"], 3),
        ("grace", 1800, ["daily.20261004T073000Z"], 3),
        ("grace", 600, [], 4),
    ],
)
def test_downtime_across_slots_follows_the_misfire_policy(
    kernel: Kernel,
    clock: Clock,
    misfire: str,
    grace: int | None,
    admitted: list[str],
    missed: int,
) -> None:
    install(kernel, spec(misfire=misfire, grace_seconds=grace))
    outcome = tick(kernel, clock, "2026-10-04T07:45:00")
    assert outcome == {"daily": "admitted" if admitted else "missed"}
    assert tasks(kernel) == admitted
    (report,) = [e for e in events(kernel) if e["kind"] == "missed"]
    assert report["detail"]["count"] == missed
    assert report["detail"]["first"] == "2026-10-01T07:30:00+00:00"
    assert report["detail"]["last"] == (
        "2026-10-03T07:30:00+00:00" if admitted else "2026-10-04T07:30:00+00:00"
    )
    if admitted:
        assert [e["detail"]["late"] for e in events(kernel) if e["kind"] == "admitted"] == [True]
    # Nothing is caught up again, and the next slot runs on time.
    assert tick(kernel, clock, "2026-10-04T08:00:00") == {"daily": "idle"}
    assert tick(kernel, clock, "2026-10-05T07:30:03") == {"daily": "admitted"}
    assert tasks(kernel) == [*admitted, "daily.20261005T073000Z"]


def test_short_lateness_is_on_time_under_every_policy(kernel: Kernel, clock: Clock) -> None:
    install(kernel, spec(misfire="skip"))
    assert tick(kernel, clock, "2026-10-01T07:31:50") == {"daily": "admitted"}


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (
            "2026-03-26T12:00:00",
            ["20260327T083000Z", "20260328T083000Z", "20260329T073000Z", "20260330T073000Z"],
        ),
        (
            "2026-10-22T12:00:00",
            ["20261023T073000Z", "20261024T073000Z", "20261025T083000Z", "20261026T083000Z"],
        ),
    ],
)
def test_dst_transition_admits_each_local_0830_once(
    kernel: Kernel, clock: Clock, start: str, expected: list[str]
) -> None:
    clock.now = at(start)
    install(kernel, spec())
    for key in expected:
        slot = datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        # 08:30 at the other UTC offset admits nothing, before or after the real slot.
        for when, outcome in ((-1, "idle"), (0, "admitted"), (1, "idle")):
            clock.now = slot + timedelta(hours=when, seconds=5)
            assert Scheduler(kernel, clock).tick() == {"daily": outcome}, clock.now
    assert tasks(kernel) == [f"daily.{key}" for key in expected]


# Lifecycle


def test_disabled_or_removed_trigger_admits_nothing_and_is_not_caught_up(
    kernel: Kernel, clock: Clock
) -> None:
    install(kernel, spec(enabled=False))
    assert tick(kernel, clock, "2026-10-01T07:30:05") == {}
    clock.now = at("2026-10-03T12:00:00")
    install(kernel, spec(misfire="run_once_late"))
    # Slots while disabled are not misfires; only slots after re-enabling count.
    assert tick(kernel, clock, "2026-10-03T12:00:10") == {"daily": "idle"}
    assert tick(kernel, clock, "2026-10-04T07:30:05") == {"daily": "admitted"}
    clock.now = at("2026-10-04T12:00:00")
    install(kernel, spec(misfire="run_once_late", enabled=False))
    assert tick(kernel, clock, "2026-10-05T07:30:05") == {}
    install(kernel)
    assert tick(kernel, clock, "2026-10-06T07:30:05") == {}
    assert tasks(kernel) == ["daily.20261004T073000Z"]
    assert kinds(kernel) == ["installed", "enabled", "admitted", "disabled", "removed"]


def test_revoked_authority_is_recorded_and_does_not_stop_other_triggers(
    kernel: Kernel, clock: Clock
) -> None:
    # `other` is refused at admission (its agent is not bound to the runtime);
    # `daily` is still admitted in the same tick.
    broken = spec(trigger_id="other", template=template(agent_id="outsider"))
    install(kernel, spec(), broken)
    assert tick(kernel, clock, "2026-10-01T07:30:05") == {"daily": "admitted", "other": "failed"}
    assert events(kernel, "other")[-1]["code"] == "runtime_binding"
    # Revoking the supervisor's grant refuses the next slot; the scheduler keeps running.
    install(kernel, spec(), rights=RIGHTS)
    assert tick(kernel, clock, "2026-10-02T07:30:05") == {"daily": "failed"}
    failed = events(kernel)[-1]
    assert failed["kind"] == "failed" and failed["code"] == "authority_denied"
    assert failed["slot"] == "2026-10-02T07:30:00+00:00"
    # The slot is decided once: no retry storm on later ticks.
    assert tick(kernel, clock, "2026-10-02T07:31:00") == {"daily": "idle"}
    # A disabled supervisor is refused the same way, even with grants restored.
    disabled = tuple(
        p.model_copy(update={"disabled": p.principal_id == "supervisor"}) for p in PRINCIPALS
    )
    provision(kernel.database, document((spec(),), principals=disabled), kernel.operations)
    assert tick(kernel, clock, "2026-10-03T07:30:05") == {"daily": "failed"}
    assert events(kernel)[-1]["code"] == "principal_disabled"
    assert tasks(kernel) == ["daily.20261001T073000Z"]


def test_template_is_revalidated_at_admission(kernel: Kernel, clock: Clock) -> None:
    install(kernel, spec())
    kernel.operations = registry(required=("to", "body", "subject"))
    assert tick(kernel, clock, "2026-10-01T07:30:05") == {"daily": "failed"}
    assert events(kernel)[-1]["code"] == "invalid_arguments"
    kernel.operations = Registry((), {})
    assert tick(kernel, clock, "2026-10-02T07:30:05") == {"daily": "failed"}
    assert events(kernel)[-1]["code"] == "operation_not_installed"
    assert tasks(kernel) == []


# Inspection


def test_triggers_route_lists_only_the_supervisors_triggers(kernel: Kernel, clock: Clock) -> None:
    install(kernel, spec())
    tick(kernel, clock, "2026-10-01T07:30:05")
    client = TestClient(create_app(kernel))

    def listed(token: str) -> dict[str, Any]:
        response = client.get(
            "/v0.2/triggers",
            headers={"Authorization": f"Bearer {token}", "X-Cardea-Auth": "spiffe"},
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())

    (entry,) = listed("supervisor")["triggers"]
    assert entry["trigger_id"] == "daily" and entry["enabled"] is True
    assert entry["last_slot"] == "2026-10-01T07:30:00+00:00"
    assert entry["definition"]["misfire"] == "skip"
    assert [e["kind"] for e in entry["events"]] == ["admitted", "installed"]
    assert listed("outsider")["triggers"] == []
    assert client.post("/v0.2/triggers", json={}).status_code == 405
