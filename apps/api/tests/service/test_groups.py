"""Agent groups (ADR-034 slice 4): apply, plan, members, delegated grants, compiled
time triggers and one run per member; portable fakes only."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from test_agent_runs import FakeSession, call

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.app import create_app
from cardea.service.auth import Authentication, Proof
from cardea.service.database import Database, Fault
from cardea.service.groups import apply
from cardea.service.harnesses import IMPLICIT_ID, Catalog, Harnesses, ModelEntry, implicit_entry
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    RIGHTS,
    AgentGroup,
    AgentRunRequest,
    Permission,
    Principal,
    Provisioning,
)
from cardea.service.operations import OperationContext, OperationDescriptor, Registry
from cardea.service.triggers import Scheduler


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


def descriptor(name: str, effect: str) -> OperationDescriptor:
    return OperationDescriptor.model_validate(
        {
            "tool": {
                "name": name,
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            },
            "cardea": {"version": 1, "effect": effect, "handler": f"test.{name}"},
        }
    )


async def ok(arguments: Any, context: OperationContext) -> dict[str, Any]:
    return {}


REGISTRY = Registry(
    (descriptor("mail.send", "effect"), descriptor("listings.search", "read")),
    {"test.mail.send": ok, "test.listings.search": ok},
)
RUN_GRANTS = (*RIGHTS, "models.invoke", "listings.search", "mail.send")
SUPERVISOR = (*RUN_GRANTS, "groups.manage")
PRINCIPALS = tuple(
    Principal(
        principal_id=p,
        kind="service" if p == "runtime" else "human",
        auth_kind="spiffe",
        issuer="test",
        subject=p,
    )
    for p in ("runtime", "supervisor", "outsider")
) + (Principal(principal_id="taken.worker.0", kind="agent", runtime_id="runtime"),)


def document(
    rights: tuple[str, ...] = SUPERVISOR,
    *,
    undelegable: tuple[str, ...] = (),
    disabled: tuple[str, ...] = (),
) -> Provisioning:
    expires = datetime.now(UTC) + timedelta(hours=1)
    granted = tuple(
        Permission(
            principal_id=who,
            namespace="test",
            operation=right,
            delegate=right not in undelegable,
            expires_at=expires,
        )
        for who in ("supervisor", "outsider")
        for right in rights
    )
    return Provisioning(
        principals=tuple(
            p.model_copy(update={"disabled": p.principal_id in disabled}) for p in PRINCIPALS
        ),
        permissions=granted,
        platform=tuple(
            p.model_copy(update={"principal_id": "platform", "delegate": False})
            for p in granted
            if p.principal_id == "supervisor"
        ),
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: FakeSession
) -> Iterator[Kernel]:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    db = Database(tmp_path / "data")
    db.migrate()
    provision(db, document(), REGISTRY)
    entry = implicit_entry("model-a").model_copy(
        update={"models": {"model-a": ModelEntry(), "model-b": ModelEntry()}}
    )
    harnesses = Harnesses(
        {IMPLICIT_ID: session}, Catalog({IMPLICIT_ID: entry}, IMPLICIT_ID, implicit=IMPLICIT_ID)
    )
    instance = Kernel(db, cast(Authentication, Auth()), operations=REGISTRY, harnesses=harnesses)
    try:
        yield instance
    finally:
        db.close()


def pool(**changes: Any) -> dict[str, Any]:
    return {
        "name": "worker",
        "replicas": 2,
        "grants": list(RUN_GRANTS),
        "run": {
            "instructions": "Search listings and email one recap.",
            "operations": ["listings.search", "mail.send"],
            "model": "model-a",
            "bounds": {"timeout_seconds": 60, "attempts": 1},
        },
        "triggers": [{"schedule": {"cron": "0 * * * *", "timezone": "UTC", "misfire": "skip"}}],
        **changes,
    }


def manifest(*pools: dict[str, Any], name: str = "desk") -> dict[str, Any]:
    return {
        "apiVersion": "cardea/v1alpha1",
        "kind": "AgentGroup",
        "metadata": {"name": name, "namespace": "test"},
        "spec": {"supervisor": "supervisor", "pools": list(pools) or [pool()]},
    }


def put(
    kernel: Kernel, body: dict[str, Any], dry_run: bool = False, who: str = "supervisor"
) -> dict[str, Any]:
    return apply(
        kernel,
        kernel.auth.validate(who),
        body["metadata"]["name"],
        AgentGroup.model_validate(body),
        dry_run,
    )


def rows(kernel: Kernel, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with kernel.database.read() as conn:
        return [tuple(r) for r in conn.exec_driver_sql(sql, args)]


def tick(kernel: Kernel, when: str) -> dict[str, str]:
    at = datetime.fromisoformat(when).replace(tzinfo=UTC)
    return Scheduler(kernel, lambda: at).tick()


@pytest.fixture
def anchored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install triggers at a fixed instant so ticks are deterministic."""
    monkeypatch.setattr(
        "cardea.service.groups.now", lambda: datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
    )


def counts(kernel: Kernel) -> tuple[int, ...]:
    return tuple(
        rows(kernel, f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("principals", "agent_groups", "group_members", "triggers", "events")
    )


def admitted(kernel: Kernel) -> list[str]:
    return [r[0] for r in rows(kernel, "SELECT task_id FROM trigger_events WHERE kind='admitted'")]


async def done(client: ClientSession, request: SessionRequest) -> SessionResult:
    return SessionResult(text="Done.", is_error=False)


# Apply


def test_apply_creates_members_grants_and_triggers(kernel: Kernel) -> None:
    result = put(kernel, manifest())
    assert result["change"] == "created" and result["generation"] == 1
    assert result["plan"]["members_added"] == ["desk.worker.0", "desk.worker.1"]
    assert result["plan"]["triggers_installed"] == ["desk.worker.0.t0", "desk.worker.1.t0"]
    # Runtime-bound agent principals with no external identity.
    assert rows(
        kernel,
        "SELECT principal_id,kind,disabled,auth_kind,issuer,subject,runtime_id FROM principals "
        "WHERE principal_id LIKE 'desk.%' ORDER BY principal_id",
    ) == [
        ("desk.worker.0", "agent", 0, None, "", "", "runtime"),
        ("desk.worker.1", "agent", 0, None, "", "", "runtime"),
    ]
    ((definition,),) = rows(
        kernel,
        "SELECT definition_json FROM triggers WHERE trigger_id='desk.worker.1.t0' "
        "AND managed_by='group' AND supervisor_id='supervisor'",
    )
    template = json.loads(definition)["template"]
    assert template["agent_id"] == "desk.worker.1"
    assert tuple(template["grants"]) == RUN_GRANTS
    assert (template["max_attempts"], template["attempt_timeout_seconds"]) == (1, 60)
    view = result["group"]
    assert view["metadata"]["generation"] == 1
    assert view["status"]["observedGeneration"] == 1
    assert [m["triggers"] for m in view["status"]["members"]] == [
        ["desk.worker.0.t0"],
        ["desk.worker.1.t0"],
    ]
    assert rows(kernel, "SELECT COUNT(*) FROM events WHERE event_type='group.applied'") == [(1,)]
    again = put(kernel, manifest())
    assert again["change"] == "unchanged" and again["generation"] == 1
    assert again["plan"] == {
        "members_added": [],
        "triggers_installed": [],
        "triggers_updated": [],
        "triggers_removed": [],
    }


def test_dry_run_returns_the_plan_and_changes_nothing(kernel: Kernel) -> None:
    before = counts(kernel)
    planned = put(kernel, manifest(), dry_run=True)
    assert planned["dry_run"] is True
    assert planned["plan"]["members_added"] == ["desk.worker.0", "desk.worker.1"]
    assert counts(kernel) == before
    put(kernel, manifest())
    applied = counts(kernel)
    scaled = put(kernel, manifest(pool(replicas=3)), dry_run=True)
    assert scaled["plan"]["members_added"] == ["desk.worker.2"]
    assert scaled["group"]["metadata"]["generation"] == 2
    assert counts(kernel) == applied
    # A dry run refuses exactly what the apply would refuse.
    with pytest.raises(Fault, match="slice 8") as refused:
        put(kernel, manifest(pool(replicas=1)), dry_run=True)
    assert refused.value.code == "group_drain_unsupported"


def test_restructure_by_manifest_only(kernel: Kernel) -> None:
    put(kernel, manifest())
    worker = pool(replicas=3)
    worker["run"] = {**worker["run"], "model": "model-b"}
    review = pool(name="review", replicas=1, triggers=[])
    result = put(kernel, manifest(worker, review))
    assert result["change"] == "updated" and result["generation"] == 2
    assert result["plan"]["members_added"] == ["desk.worker.2", "desk.review.0"]
    assert result["plan"]["triggers_installed"] == ["desk.worker.2.t0"]
    assert result["plan"]["triggers_updated"] == ["desk.worker.0.t0", "desk.worker.1.t0"]
    models = rows(
        kernel,
        "SELECT json_extract(definition_json,'$.template.model') FROM triggers "
        "WHERE managed_by='group' AND removed=0",
    )
    assert models == [("model-b",)] * 3
    assert result["group"]["status"]["observedGeneration"] == 2
    # Dropping a manifest trigger removes the compiled triggers; the members stay.
    removed = put(kernel, manifest({**worker, "triggers": []}, review))
    assert removed["plan"]["triggers_removed"] == [
        "desk.worker.0.t0",
        "desk.worker.1.t0",
        "desk.worker.2.t0",
    ]
    assert len(removed["group"]["status"]["members"]) == 4


def test_scale_down_pool_removal_and_deletion_wait_for_slice_8(kernel: Kernel) -> None:
    put(kernel, manifest())
    for body in (manifest(pool(replicas=1)), manifest(pool(name="other"))):
        with pytest.raises(Fault) as refused:
            put(kernel, body)
        assert refused.value.code == "group_drain_unsupported" and refused.value.status == 409
    client = TestClient(create_app(kernel))
    response = client.delete(
        "/v0.2/groups/desk",
        params={"namespace": "test"},
        headers={"Authorization": "Bearer supervisor"},
    )
    assert response.status_code == 409 and response.json()["code"] == "group_drain_unsupported"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"spend": {"usd": 10, "window": "day"}}, "group_spend_unsupported"),
        (
            {"expect_window": [{"operation": "mail.send", "min": 1, "window": "day"}]},
            "group_expect_window_unsupported",
        ),
        (
            {"triggers": [{"event": {"types": ["cardea.mailbox.sent"]}}]},
            "group_event_trigger_unsupported",
        ),
    ],
)
def test_later_slices_are_refused_not_ignored(
    kernel: Kernel, change: dict[str, Any], code: str
) -> None:
    with pytest.raises(Fault) as refused:
        put(kernel, manifest(pool(**change)))
    assert refused.value.code == code and refused.value.status == 422


def test_apply_requires_the_supervisor_and_groups_manage(kernel: Kernel) -> None:
    with pytest.raises(Fault, match="own groups"):
        put(kernel, manifest(), who="outsider")
    provision(kernel.database, document(RUN_GRANTS), REGISTRY)
    with pytest.raises(Fault) as refused:
        put(kernel, manifest())
    assert refused.value.code == "authority_denied"
    with pytest.raises(Fault, match="differ"):
        apply(
            kernel,
            kernel.auth.validate("supervisor"),
            "other",
            AgentGroup.model_validate(manifest()),
            False,
        )


def test_members_cannot_take_an_existing_principal(kernel: Kernel) -> None:
    with pytest.raises(Fault) as refused:
        put(kernel, manifest(name="taken"))
    assert refused.value.code == "principal_conflict"
    assert rows(kernel, "SELECT COUNT(*) FROM agent_groups") == [(0,)]


def test_http_routes_apply_plan_and_read(kernel: Kernel) -> None:
    client = TestClient(create_app(kernel))
    auth = {"Authorization": "Bearer supervisor"}
    planned = client.put("/v0.2/groups/desk?dry_run=true", json=manifest(), headers=auth)
    assert planned.status_code == 200 and planned.json()["dry_run"] is True
    assert client.get("/v0.2/groups/desk?namespace=test", headers=auth).status_code == 404
    assert client.put("/v0.2/groups/desk", json=manifest(), headers=auth).status_code == 200
    view = client.get("/v0.2/groups/desk?namespace=test", headers=auth).json()
    assert view["apiVersion"] == "cardea/v1alpha1" and view["kind"] == "AgentGroup"
    # A GET body applies again unchanged (the Kubernetes round trip).
    again = client.put("/v0.2/groups/desk", json=view, headers=auth)
    assert again.status_code == 200 and again.json()["change"] == "unchanged"
    other = client.get(
        "/v0.2/groups/desk?namespace=test", headers={"Authorization": "Bearer outsider"}
    )
    assert other.status_code == 404


# Authority ceiling


def test_ceiling_is_checked_at_apply_with_pool_and_grant(kernel: Kernel) -> None:
    provision(kernel.database, document(undelegable=("mail.send",)), REGISTRY)
    with pytest.raises(Fault, match="Pool worker grant mail.send") as refused:
        put(kernel, manifest())
    assert refused.value.code == "group_grant_denied"
    assert rows(kernel, "SELECT COUNT(*) FROM principals WHERE principal_id LIKE 'desk.%'") == [
        (0,)
    ]


def test_ceiling_is_checked_at_every_run(kernel: Kernel, anchored: None) -> None:
    put(kernel, manifest(pool(replicas=1)))
    provision(kernel.database, document(undelegable=("mail.send",)), REGISTRY)
    assert tick(kernel, "2026-10-01T07:00:10") == {"desk.worker.0.t0": "failed"}
    assert rows(kernel, "SELECT code FROM trigger_events WHERE kind='failed'") == [
        ("authority_denied",)
    ]


def test_revoking_groups_manage_stops_members(kernel: Kernel, anchored: None) -> None:
    put(kernel, manifest(pool(replicas=1)))
    provision(kernel.database, document(RUN_GRANTS), REGISTRY)
    assert tick(kernel, "2026-10-01T07:00:10") == {"desk.worker.0.t0": "failed"}
    assert rows(kernel, "SELECT code FROM trigger_events WHERE kind='failed'") == [
        ("authority_denied",)
    ]
    provision(kernel.database, document(), REGISTRY)
    assert tick(kernel, "2026-10-01T08:00:10") == {"desk.worker.0.t0": "admitted"}


def test_disabled_supervisor_stops_members(
    kernel: Kernel, session: FakeSession, anchored: None
) -> None:
    put(kernel, manifest(pool(replicas=1)))
    assert tick(kernel, "2026-10-01T07:00:10") == {"desk.worker.0.t0": "admitted"}
    (task_id,) = admitted(kernel)

    async def revoked(client: ClientSession, request: SessionRequest) -> SessionResult:
        provision(kernel.database, document(disabled=("supervisor",)), REGISTRY)
        refused = await call(client, "mail-send")
        assert refused.is_error
        return SessionResult(text="Could not send.", is_error=False)

    session.scripts = [revoked]
    AgentRunRuntime(kernel).execute(task_id)
    assert rows(
        kernel,
        "SELECT COUNT(*) FROM operations WHERE task_id=? "
        "AND json_extract(snapshot,'$.request.name')='mail.send' "
        "AND json_extract(snapshot,'$.state')='succeeded'",
        task_id,
    ) == [(0,)]
    assert tick(kernel, "2026-10-01T08:00:10") == {"desk.worker.0.t0": "failed"}
    assert rows(kernel, "SELECT code FROM trigger_events WHERE kind='failed'") == [
        ("principal_disabled",)
    ]


# One run per member


def test_slots_coalesce_while_the_members_run_is_unfinished(
    kernel: Kernel, session: FakeSession, anchored: None
) -> None:
    put(kernel, manifest(pool(replicas=1)))
    assert tick(kernel, "2026-10-01T07:00:10") == {"desk.worker.0.t0": "admitted"}
    (first,) = admitted(kernel)
    assert tick(kernel, "2026-10-01T08:00:10") == {"desk.worker.0.t0": "coalesced"}
    assert tick(kernel, "2026-10-01T08:00:40") == {"desk.worker.0.t0": "idle"}
    assert rows(kernel, "SELECT slot,task_id,code FROM trigger_events WHERE kind='coalesced'") == [
        ("2026-10-01T08:00:00+00:00", first, "member_busy")
    ]
    session.scripts = [done]
    assert AgentRunRuntime(kernel).execute(first) == "succeeded"
    assert tick(kernel, "2026-10-01T09:00:10") == {"desk.worker.0.t0": "admitted"}
    assert len(admitted(kernel)) == 2


def test_member_runs_are_bound_to_the_group_supervisor(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=1)))
    request = AgentRunRequest.model_validate(
        {
            "request_id": "manual-1",
            "agent_id": "desk.worker.0",
            "namespace": "test",
            "objective": "Manual run",
            "instructions": "Do it.",
            "operations": ["listings.search"],
            "grants": [*RIGHTS, "models.invoke", "listings.search"],
        }
    )
    with pytest.raises(Fault) as refused:
        kernel.submit(kernel.auth.validate("outsider"), request)
    assert refused.value.code == "member_binding"
    kernel.submit(kernel.auth.validate("supervisor"), request)
    with pytest.raises(Fault) as busy:
        kernel.submit(
            kernel.auth.validate("supervisor"), request.model_copy(update={"request_id": "m-2"})
        )
    assert busy.value.code == "member_busy" and busy.value.status == 409


def test_history_and_members_stay_append_only(kernel: Kernel) -> None:
    put(kernel, manifest(pool(replicas=1)))
    with pytest.raises(Exception, match="append-only"), kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE trigger_events SET code='x'")
    with pytest.raises(Exception, match="never deleted"), kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM group_members")
