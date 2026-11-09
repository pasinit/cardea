"""Kernel journal events as an event-trigger source; group event triggers (ADR-034 slice 7)."""

from datetime import timedelta
from typing import Any

import pytest
import test_agent_runs as runs
from mcp.client.session import ClientSession
from test_mailboxes import (
    GRANTS,
    INBOX,
    fails,
    kernel,
    mail_rows,
    note,
    session,
    succeeds,
)

from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.service import agent_run_runtime, journal, mailbox
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.database import borrow
from cardea.service.groups import apply
from cardea.service.kernel import Kernel, provision
from cardea.service.models import (
    AgentGroup,
    Permission,
    Principal,
    Provisioning,
    TriggerSpec,
    now,
)

__all__ = ["kernel", "session"]


def team(kernel: Kernel, **event: Any) -> None:
    """Group `team`, pool `dev` (2 members) woken by mail to the member or its pool."""
    trigger = {"types": ["cardea.mailbox.sent"], "subjects": ["$member", "$pool"], **event}
    apply(
        kernel,
        kernel.auth.validate("supervisor"),
        "team",
        AgentGroup.model_validate(
            {
                "apiVersion": "cardea/v1alpha1",
                "kind": "AgentGroup",
                "metadata": {"name": "team", "namespace": "test"},
                "spec": {
                    "supervisor": "supervisor",
                    "pools": [
                        {
                            "name": "dev",
                            "replicas": 2,
                            "grants": list(GRANTS),
                            "run": {
                                "instructions": "Handle your mail.",
                                "operations": ["mailbox.send"],
                                "context": INBOX,
                                "bounds": {"timeout_seconds": 30, "attempts": 2},
                            },
                            "triggers": [{"event": trigger}],
                        }
                    ],
                },
            }
        ),
        False,
    )


def post(kernel: Kernel, to: dict[str, str], sender: str = "supervisor", n: int = 1) -> None:
    """Commit mail as `sender` would, without its API or tool call (tested elsewhere)."""
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        for _ in range(n):
            mailbox.send(
                conn,
                tx,
                namespace="test",
                sender_id=sender,
                supervisor_id="supervisor",
                to=mailbox.Address.model_validate(to),
                parts=note(),
                runtime_id="runtime",
                at=now(),
            )


def tick(kernel: Kernel) -> dict[str, str]:
    return journal.Dispatcher(kernel).tick()


def deliveries(kernel: Kernel, trigger: str = "team.dev.0.t0") -> list[tuple[str, str | None]]:
    with kernel.database.read() as conn:
        return [
            (str(r[0]), r[1])
            for r in conn.exec_driver_sql(
                "SELECT outcome,code FROM trigger_deliveries WHERE trigger_id=? "
                "ORDER BY delivery_id",
                (trigger,),
            )
        ]


def admitted(kernel: Kernel, agent: str = "team.dev.0") -> list[str]:
    with kernel.database.read() as conn:
        return [
            str(r[0])
            for r in conn.exec_driver_sql(
                "SELECT task_id FROM task_bindings WHERE agent_id=? ORDER BY rowid", (agent,)
            )
        ]


def test_mail_to_a_member_wakes_it_once(kernel: Kernel, session: runs.FakeSession) -> None:
    team(kernel)
    post(kernel, {"instance": "team.dev.0"})
    tick(kernel)
    (task_id,) = admitted(kernel)
    assert admitted(kernel, "team.dev.1") == []
    assert deliveries(kernel) == [("admitted", None)]
    # A second tick finds nothing new; the event is admitted once.
    tick(kernel)
    assert admitted(kernel) == [task_id] and deliveries(kernel) == [("admitted", None)]
    session.scripts = [succeeds]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    request = session.requests[0]
    assert agent_run_runtime.EVENT_PREFACE in request.prompt
    assert '"type": "cardea.mailbox.sent"' in request.prompt
    assert '"source": "cardea://test"' in request.prompt
    assert [r["state"] for r in mail_rows(kernel)] == ["consumed"]
    # Consuming emitted `delivered`, which this trigger does not match.
    tick(kernel)
    assert admitted(kernel) == [task_id]


def test_pool_mail_wakes_every_member_subscribed_to_its_pool(kernel: Kernel) -> None:
    team(kernel)
    post(kernel, {"pool": "team.dev"})
    tick(kernel)
    assert len(admitted(kernel, "team.dev.0")) == len(admitted(kernel, "team.dev.1")) == 1


def test_a_burst_coalesces_into_the_unstarted_run(
    kernel: Kernel, session: runs.FakeSession
) -> None:
    team(kernel)
    post(kernel, {"instance": "team.dev.0"}, n=5)
    tick(kernel)
    (task_id,) = admitted(kernel)
    assert deliveries(kernel) == [("admitted", None)] + [("refused", "coalesced")] * 4
    session.scripts = [succeeds]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    assert len(session.requests) == 1
    assert [r["state"] for r in mail_rows(kernel)] == ["consumed"] * 5
    # The run had not started when the burst coalesced, so no follow-up is owed.
    tick(kernel)
    assert admitted(kernel) == [task_id]


def test_mail_coalesced_into_a_started_run_gets_one_follow_up(
    kernel: Kernel, session: runs.FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_run_runtime, "attempt_lease_seconds", lambda request: 4.0)
    monkeypatch.setattr(agent_run_runtime, "LEASE_MARGIN", 1.0)

    class Crash(Exception):
        pass

    def crash(stage: str) -> None:
        if stage == "agent_run.attempt_started":
            raise Crash

    team(kernel)
    post(kernel, {"instance": "team.dev.0"})
    tick(kernel)
    (first,) = admitted(kernel)
    kernel.fault = crash
    with pytest.raises(Crash):
        AgentRunRuntime(kernel).execute(first)
    kernel.fault = lambda stage: None
    # The run started (its mail is leased); two more messages arrive while it is unfinished.
    post(kernel, {"instance": "team.dev.0"}, n=2)
    tick(kernel)
    assert admitted(kernel) == [first]
    assert deliveries(kernel)[1:] == [("refused", "coalesced")] * 2
    session.scripts = [succeeds, succeeds]
    assert AgentRunRuntime(kernel).execute(first) == "succeeded"
    # One follow-up run for what arrived after the first run started; here the retried
    # attempt already leased that mail, so the follow-up finds the mailbox empty.
    tick(kernel)
    tick(kernel)
    (_, follow_up) = admitted(kernel)
    assert AgentRunRuntime(kernel).execute(follow_up) == "succeeded"
    assert [r["state"] for r in mail_rows(kernel)] == ["consumed"] * 3


def test_a_crash_around_the_commit_never_double_admits(kernel: Kernel) -> None:
    team(kernel)
    post(kernel, {"instance": "team.dev.0"})

    def before(stage: str) -> None:
        if stage == "journal.before_commit":
            raise RuntimeError("process lost before the admission commit")

    kernel.fault = before
    assert tick(kernel) == {"team.dev.0.t0": "error", "team.dev.1.t0": "idle"}
    assert admitted(kernel) == [] and deliveries(kernel) == []

    def after(stage: str) -> None:
        if stage == "journal.after_commit":
            raise RuntimeError("process lost after the admission commit")

    kernel.fault = after
    tick(kernel)
    kernel.fault = lambda stage: None
    tick(kernel)
    assert len(admitted(kernel)) == 1 and deliveries(kernel) == [("admitted", None)]


def test_revoking_the_ceiling_stops_admissions(kernel: Kernel) -> None:
    team(kernel)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*GRANTS, "groups.manage")
        if right != "mailbox.inbox"
    )
    provision(
        kernel.database,
        Provisioning(
            principals=(
                Principal(
                    principal_id="supervisor",
                    kind="human",
                    auth_kind="spiffe",
                    issuer="test",
                    subject="supervisor",
                ),
            ),
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
    )
    post(kernel, {"instance": "team.dev.0"})
    tick(kernel)
    assert admitted(kernel) == []
    assert deliveries(kernel) == [("refused", "authority_denied")]


def test_a_member_does_not_wake_itself(kernel: Kernel, session: runs.FakeSession) -> None:
    team(kernel)

    async def sends(client: ClientSession, request: SessionRequest) -> SessionResult:
        sent = await runs.call(
            client, "mailbox-send", client_key="k", to={"pool": "team.dev"}, parts=note()
        )
        assert not sent.is_error, sent.content
        return SessionResult(text="Asked the pool.", is_error=False)

    post(kernel, {"instance": "team.dev.0"})
    tick(kernel)
    (task_id,) = admitted(kernel)
    session.scripts = [sends]
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    tick(kernel)
    # Its own pool mail woke its peer, not itself.
    assert admitted(kernel) == [task_id]
    assert len(admitted(kernel, "team.dev.1")) == 1


def test_include_own_lets_a_member_subscribe_to_its_own_events(
    kernel: Kernel, session: runs.FakeSession
) -> None:
    team(kernel, include_own=True)
    post(kernel, {"instance": "team.dev.0"}, sender="team.dev.0")
    tick(kernel)
    assert len(admitted(kernel)) == 1


def test_failed_runs_redeliver_without_waking_anyone(
    kernel: Kernel, session: runs.FakeSession
) -> None:
    team(kernel)
    post(kernel, {"instance": "team.dev.0"})
    tick(kernel)
    (task_id,) = admitted(kernel)
    session.scripts = [fails, fails]
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    tick(kernel)
    assert admitted(kernel) == [task_id]


def test_journal_triggers_are_namespace_local_and_unsigned() -> None:
    spec = TriggerSpec.model_validate(
        {
            "trigger_id": "watch",
            "supervisor_id": "supervisor",
            "journal": {"types": ["cardea.record.created"]},
            "template_kind": "agent-run",
            "template": {
                "agent_id": "bob",
                "namespace": "test",
                "objective": "o",
                "instructions": "i",
                "operations": ["mailbox.send"],
                "grants": list(GRANTS),
            },
        }
    )
    assert spec.kind == "journal" and spec.event is None
    assert journal.source("test") == "cardea://test"
    with pytest.raises(ValueError):
        TriggerSpec.model_validate({**spec.model_dump(mode="json"), "schedule": "0 * * * *"})


def test_approval_events_are_exposed_and_large_data_is_bounded(kernel: Kernel) -> None:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        events: tuple[tuple[str, dict[str, Any]], ...] = (
            ("approval.requested", {"request_id": "req-1", "operation": "mail.send"}),
            ("approval.decided", {"request_id": "req-1", "note": "x" * 5000}),
            ("approval.req-1", {"request_id": "req-1"}),
            ("task.bound", {}),
        )
        for kind, payload in events:
            tx.append_event(
                namespace="test",
                event_type=kind,
                producer_id="supervisor",
                correlation_id="req-1",
                at=now(),
                payload=payload,
            )
        rows = conn.exec_driver_sql(
            "SELECT * FROM events WHERE namespace='test' ORDER BY cursor"
        ).mappings()
        adapted = [journal.adapt(conn, row, "runtime") for row in rows]
    exposed = [a for a in adapted if a is not None]
    assert [(e.type, e.subject, attrs["actor"]) for e, attrs in exposed] == [
        ("cardea.approval.requested", "req-1", "supervisor"),
        ("cardea.approval.decided", "req-1", "supervisor"),
    ]
    data = exposed[1][0].data
    assert isinstance(data, dict) and data["truncated"] is True
    assert isinstance(data["bytes"], int) and data["bytes"] > journal.MAX_DATA_BYTES
