"""A supervisor's read-only team status, section by section, on the worked examples."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_worked_examples import (
    DESK_POOLS,
    ENG_POOLS,
    RUN,
    configure,
    example_collections,
    example_group,
    setup,
)

from cardea.service import team_status
from cardea.service.app import create_app
from cardea.service.kernel import Kernel
from cardea.service.models import AgentGroup, AgentRunRequest, Signal

__all__ = ["setup"]

STEPS = iter(range(10**6))
SECTIONS = ("work", "runs", "mail", "changes", "spend", "standing")
READABLE = (*SECTIONS, "mail/text")


def status(kernel: Kernel, section: str, who: str | None = "sup-eng", **params: Any) -> Any:
    client = TestClient(create_app(kernel))
    headers = {"Authorization": f"Bearer {who}", "X-Cardea-Auth": "spiffe"} if who else {}
    return client.get(
        f"/v0.2/groups/{params.pop('group', 'eng-team')}/status/{section}",
        params={"namespace": params.pop("namespace", "eng"), **params},
        headers=headers,
    )


def receipt(
    kernel: Kernel,
    operation: str,
    arguments: dict[str, Any],
    output: dict[str, Any],
    *,
    agent: str,
    at: str,
    task_id: str,
    state: str = "succeeded",
) -> None:
    """A kernel receipt as `vcs.*` leaves it, planted on a real task of the group."""
    receipt_id = f"rcpt-{operation}-{at}-{arguments.get('number')}-{agent}"
    snapshot = {
        "receipt_id": receipt_id,
        "created_at": at,
        "updated_at": at,
        "state": state,
        "request": {
            "name": operation,
            "resource": {"namespace": "eng"},
            "arguments": {
                "arguments": arguments,
                "context": {"agent_id": agent, "group": "eng-team"},
            },
        },
        "result": {"output": output},
    }
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO operations VALUES (?,?,?,?,?,?)",
            (task_id, receipt_id, receipt_id, state, 1, json.dumps(snapshot)),
        )


def submit(
    kernel: Kernel, supervisor: str, namespace: str, member: str, grants: tuple[str, ...]
) -> str:
    """A run of a group member, admitted and queued: nothing executes it."""
    request = AgentRunRequest.model_validate(
        {
            "request_id": f"{member}.status-{next(STEPS)}",
            "agent_id": member,
            "namespace": namespace,
            "objective": "One step of the worked example",
            "instructions": "Do your step.",
            "operations": ["records.create"],
            "grants": [*RUN, *grants],
            "model": "model-a",
            "max_attempts": 1,
            "attempt_timeout_seconds": 60,
        }
    )
    return kernel.submit(kernel.auth.validate(supervisor), request).task.task_id


def cancel(kernel: Kernel, supervisor: str, task_id: str) -> None:
    signal = Signal(request_id=f"cancel-{task_id}")
    kernel.control(kernel.auth.validate(supervisor), task_id, signal, "cancel")


def record(kernel: Kernel, record_id: str, state: str, title: str, holder: str | None) -> None:
    lease = "2999-01-01T00:00:00+00:00" if holder else None
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO records(namespace,collection,record_id,data_json,state,holder,"
            "claim_epoch,lease_expires_at,version,created_by,created_at,updated_at) "
            "VALUES ('eng','work',?,?,?,?,?,?,1,'eng-team.product.0',?,?)",
            (
                record_id,
                json.dumps({"title": title}),
                state,
                holder,
                1 if holder else 0,
                lease,
                f"2026-11-2{len(record_id)}T09:00:00+00:00",
                "2026-11-22T09:00:00+00:00",
            ),
        )


def message(
    kernel: Kernel,
    message_id: str,
    sender: str,
    kind: str,
    address: str,
    at: str,
    body: str,
    item: dict[str, str] | None = None,
) -> None:
    """A message and its first delivery, as `mailbox.send` stores them."""
    pool = address if kind == "pool" else None
    recipient = address if kind == "instance" else None
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO mailbox_messages(message_id,namespace,kind,sender_id,supervisor_id,"
            "request_sha256,to_kind,to_address,parts_json,body_bytes,max_deliveries,created_at,"
            "record_json,client_key) VALUES (?,'eng','message',?,'sup-eng','x',?,?,?,?,3,?,?,?)",
            (
                message_id,
                sender,
                kind,
                address,
                json.dumps([{"text": body}]),
                len(body),
                at,
                json.dumps(item) if item else None,
                f"ck-secret-{message_id}",
            ),
        )
        if kind != "group":
            conn.exec_driver_sql(
                "INSERT INTO mailbox_deliveries(message_id,namespace,recipient_id,pool,state,"
                "updated_at) VALUES (?,'eng',?,?,'pending',?)",
                (message_id, recipient, pool, at),
            )


@pytest.fixture
def team(setup: tuple[Kernel, Any, Any]) -> tuple[Kernel, str, str]:
    """The software team part-way through its work: one item claimed, one open, two runs
    cancelled and one still queued, one message to the developers."""
    kernel, _, _ = setup
    group = example_group("software-team")
    configure(kernel, "eng", "sup-eng", example_collections("software-team"), group)
    for member in ("eng-team.product.0", "eng-team.lead.0"):
        grants = ENG_POOLS[member.split(".")[1]][1]
        cancel(kernel, "sup-eng", submit(kernel, "sup-eng", "eng", member, grants))
    submit(kernel, "sup-eng", "eng", "eng-team.dev.0", ENG_POOLS["dev"][1])
    record(kernel, "rec-a", "in_progress", "a", "eng-team.dev.0")
    record(kernel, "rec-bb", "open", "b", None)
    message(
        kernel,
        "00000000-0000-0000-0000-000000000001",
        "eng-team.lead.0",
        "pool",
        "eng-team.dev",
        "2026-11-22T10:00:00+00:00",
        "Item a is ready.",
    )
    return kernel, "rec-a", "rec-bb"


def a_task(kernel: Kernel) -> str:
    with kernel.database.read() as conn:
        return str(conn.exec_driver_sql("SELECT task_id FROM task_bindings LIMIT 1").scalar_one())


@pytest.fixture
def changes(team: tuple[Kernel, str, str]) -> Kernel:
    """Three pull requests: #1 merged after approval, #2 open with changes requested then
    an approval, #3 open and unreviewed; an unsuccessful open and another group's are ignored."""
    kernel, _, _ = team
    task = a_task(kernel)
    day = "2026-11-2"
    for number, opened, title in ((1, "1", "Add login"), (2, "2", "Fix cache"), (3, "3", "Docs")):
        receipt(
            kernel,
            "vcs.open_change",
            {"repository": "cardea", "title": title, "head": f"agent/eng-team/{number}"},
            {"number": number, "url": f"https://git.test/cardea/pull/{number}"},
            agent="eng-team.dev.0",
            at=f"{day}{opened}T10:00:00+00:00",
            task_id=task,
        )
    for number, event, at, who in (
        (2, "CHANGES_REQUESTED", "2026-11-22T11:00:00+00:00", "eng-team.principal.0"),
        (2, "APPROVED", "2026-11-22T12:00:00+00:00", "eng-team.principal.0"),
        (1, "APPROVED", "2026-11-21T11:00:00+00:00", "eng-team.principal.0"),
        (3, "COMMENTED", "2026-11-23T11:00:00+00:00", "eng-team.principal.0"),
    ):
        receipt(
            kernel,
            "vcs.review",
            {"repository": "cardea", "number": number, "event": event},
            {"review_id": number * 10, "state": event, "commit_id": "abc"},
            agent=who,
            at=at,
            task_id=task,
        )
    receipt(
        kernel,
        "vcs.merge",
        {"repository": "cardea", "number": 1},
        {"number": 1, "merged": True, "sha": "deadbeef"},
        agent="eng-team.dev.0",
        at="2026-11-21T12:00:00+00:00",
        task_id=task,
    )
    receipt(
        kernel,
        "vcs.open_change",
        {"repository": "cardea", "title": "Failed", "head": "agent/eng-team/9"},
        {"number": 9, "url": ""},
        agent="eng-team.dev.0",
        at="2026-11-24T10:00:00+00:00",
        task_id=task,
        state="failed",
    )
    return kernel


def test_work_lists_items_by_state_and_holder(team: tuple[Kernel, str, str]) -> None:
    kernel, item, other = team
    view = status(kernel, "work").json()
    assert (view["group"], view["namespace"]) == ("eng-team", "eng") and view["as_of"]
    assert view["by_state"]["work"] == {"in_progress": 1, "open": 1}
    assert view["by_holder"] == [{"holder": "eng-team.dev.0", "pool": "dev", "count": 1}]
    by_id = {i["record_id"]: i for i in view["items"]}
    assert set(by_id) == {item, other}
    assert (by_id[item]["state"], by_id[item]["holder"]) == ("in_progress", "eng-team.dev.0")
    assert by_id[item]["title"] == "a" and by_id[item]["claim_epoch"] == 1
    assert (by_id[other]["state"], by_id[other]["holder"]) == ("open", None)
    assert view["next"] is None
    only = status(kernel, "work", state="open").json()["items"]
    assert [i["record_id"] for i in only] == [other]
    held = status(kernel, "work", holder="eng-team.dev.0").json()["items"]
    assert [i["record_id"] for i in held] == [item]
    assert status(kernel, "work", collection="decisions").json()["items"] == []
    refused = status(kernel, "work", collection="orders")
    assert refused.status_code == 404 and refused.json()["code"] == "collection_not_found"


def test_work_pages_follow_next(team: tuple[Kernel, str, str]) -> None:
    kernel, item, other = team
    first = status(kernel, "work", limit=1).json()
    assert len(first["items"]) == 1 and first["next"] is not None
    # Counts cover the whole group, not the page.
    assert first["by_state"]["work"] == {"in_progress": 1, "open": 1}
    second = status(kernel, "work", limit=1, after=first["next"]).json()
    assert len(second["items"]) == 1 and second["next"] is None
    assert {first["items"][0]["record_id"], second["items"][0]["record_id"]} == {item, other}
    assert status(kernel, "work", limit=101).status_code == 422
    assert status(kernel, "work", limit=0).status_code == 422


def test_work_bounds_the_holder_summary(
    team: tuple[Kernel, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, _, _ = team
    monkeypatch.setattr(team_status, "MAX_HOLDERS", 0)
    view = status(kernel, "work").json()
    assert view["by_holder"] == [] and view["by_holder_truncated"] == 1


def plant_cost(kernel: Kernel, task_id: str, usd: float) -> None:
    """One attempt's reported cost, as a finished attempt records it."""
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO run_attempt_costs VALUES (?,1,?,'2026-11-22T10:00:00+00:00')",
            (task_id, usd),
        )
        conn.exec_driver_sql(
            "UPDATE tasks SET snapshot=json_set(snapshot,'$.checkpoint',"
            'json(\'{"state":{"attempt":1}}\')) WHERE task_id=?',
            (task_id,),
        )


def test_runs_show_outcome_and_cost_newest_first(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    view = status(kernel, "runs").json()
    assert view["by_state"] == {"cancelled": 2, "queued": 1}
    items = view["items"]
    assert [i["created_at"] for i in items] == sorted(
        (i["created_at"] for i in items), reverse=True
    )
    assert [i["member"] for i in items][0] == "eng-team.dev.0"
    queued, *finished = items
    assert queued["pool"] == "dev" and queued["state"] == "queued"
    assert queued["outcome"] is None and queued["failure_code"] is None
    assert {i["member"] for i in finished} == {"eng-team.product.0", "eng-team.lead.0"}
    assert all(i["outcome"] == i["state"] == "cancelled" for i in finished)
    # No attempt reported a cost, so none is claimed; the reservation stays visible.
    assert queued["cost_usd"] is None
    assert all(i["reservation"] is None and i["reserved_usd"] is None for i in items)
    only = status(kernel, "runs", active=True).json()
    assert [i["task_id"] for i in only["items"]] == [queued["task_id"]]
    assert only["by_state"] == view["by_state"]


def test_runs_report_the_cost_an_attempt_reported(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    done = status(kernel, "runs").json()["items"][1]["task_id"]
    plant_cost(kernel, done, 0.25)
    items = {i["task_id"]: i for i in status(kernel, "runs").json()["items"]}
    assert items[done]["cost_usd"] == 0.25
    assert sum(i["cost_usd"] is None for i in items.values()) == 2


def test_runs_page_and_refuse_a_foreign_cursor(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    seen: list[str] = []
    after = ""
    while True:
        page = status(kernel, "runs", limit=2, after=after).json()
        assert len(page["items"]) <= 2
        seen += [i["task_id"] for i in page["items"]]
        if page["next"] is None:
            break
        after = page["next"]
    assert len(seen) == len(set(seen)) == 3
    refused = status(kernel, "runs", after="task-" + "0" * 32)
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_cursor"


def test_mail_shows_messages_between_roles_without_bodies(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    view = status(kernel, "mail").json()
    assert view["total"] == 1 and view["next"] is None
    (message,) = view["items"]
    assert message["from"] == {"address": "eng-team.lead.0", "role": "lead"}
    assert message["to"] == {"kind": "pool", "address": "eng-team.dev", "role": "dev"}
    assert message["deliveries"] == {"pending": 1}
    assert message["kind"] == "message" and message["body_bytes"] > 0
    assert "Item a is ready" not in json.dumps(view)
    assert "parts" not in message


def test_mail_pages_and_refuses_a_foreign_cursor(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    message(
        kernel,
        "00000000-0000-0000-0000-000000000002",
        "sup-eng",
        "group",
        "eng-team",
        "2026-11-21T00:00:00+00:00",
        "Welcome.",
    )
    # A team whose name only starts the same is not this group's.
    other = "00000000-0000-0000-0000-000000000003"
    message(kernel, other, "sup-eng", "group", "eng-team-other", "2026-11-23T00:00:00+00:00", "No")
    first = status(kernel, "mail", limit=1).json()
    assert first["total"] == 2 and first["next"] is not None
    # Newest first: the supervisor's note to the whole group is older than the lead's mail.
    assert first["items"][0]["from"]["role"] == "lead"
    second = status(kernel, "mail", limit=1, after=first["next"]).json()
    (note,) = second["items"]
    assert note["from"] == {"address": "sup-eng", "role": "supervisor"}
    assert note["to"] == {"kind": "group", "address": "eng-team", "role": None}
    assert note["deliveries"] == {} and second["next"] is None
    refused = status(kernel, "mail", after=other)
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_cursor"


def test_mail_text_returns_full_text_unmodified_and_no_secrets(
    team: tuple[Kernel, str, str],
) -> None:
    kernel, _, _ = team
    markup = '<script>alert(1)</script> <a href="javascript:x">y</a> & "q"'
    message(
        kernel,
        "00000000-0000-0000-0000-000000000009",
        "eng-team.dev.0",
        "instance",
        "eng-team.lead.0",
        "2026-11-22T11:00:00+00:00",
        markup,
        {"collection": "work", "record_id": "rec-a"},
    )
    view = status(kernel, "mail/text").json()
    assert view["total"] == 2
    newest, oldest = view["items"]
    assert newest["parts"] == [{"text": markup}]
    assert newest["item"] == {"collection": "work", "record_id": "rec-a"}
    assert oldest["parts"] == [{"text": "Item a is ready."}] and oldest["item"] is None
    assert newest["from"]["role"] == "dev" and newest["to"]["role"] == "lead"
    assert "ck-secret" not in json.dumps(view) and "client_key" not in json.dumps(view)


def test_mail_text_filters_by_member_and_item(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    second = "00000000-0000-0000-0000-000000000009"
    message(
        kernel,
        second,
        "eng-team.dev.0",
        "instance",
        "eng-team.lead.0",
        "2026-11-22T11:00:00+00:00",
        "Done.",
        {"collection": "work", "record_id": "rec-a"},
    )

    def ids(**params: str) -> list[str]:
        return [i["message_id"] for i in status(kernel, "mail/text", **params).json()["items"]]

    assert ids(member="eng-team.lead.0") == [second, "00000000-0000-0000-0000-000000000001"]
    assert ids(member="eng-team.dev.0") == [second]
    assert ids(member="eng-team.product.0") == []
    assert ids(item="work/rec-a") == [second]
    assert ids(item="work/rec-bb") == []
    assert ids(member="eng-team.lead.0", item="work/rec-a") == [second]
    assert status(kernel, "mail/text", item="not an item").status_code == 422
    assert status(kernel, "mail/text", member="a b").status_code == 422
    filtered = status(kernel, "mail/text", item="work/rec-a").json()
    assert filtered["total"] == 1


def test_mail_text_pages_and_refuses_a_cursor_outside_the_filter(
    team: tuple[Kernel, str, str],
) -> None:
    kernel, _, _ = team
    for n in range(2, 5):
        message(
            kernel,
            f"00000000-0000-0000-0000-00000000000{n}",
            "eng-team.lead.0",
            "pool",
            "eng-team.dev",
            f"2026-11-22T1{n}:00:00+00:00",
            f"Note {n}",
        )
    seen: list[str] = []
    after = ""
    while True:
        page = status(kernel, "mail/text", limit=2, after=after).json()
        assert len(page["items"]) <= 2 and page["total"] == 4
        seen += [i["message_id"] for i in page["items"]]
        if page["next"] is None:
            break
        after = page["next"]
    assert len(seen) == len(set(seen)) == 4
    assert seen[-1] == "00000000-0000-0000-0000-000000000001"
    refused = status(kernel, "mail/text", member="eng-team.product.0", after=seen[0])
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_cursor"


def test_mail_text_pages_stay_under_the_response_cap(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    for n in range(20):
        message(
            kernel,
            f"00000000-0000-0000-0001-0000000000{n:02d}",
            "eng-team.lead.0",
            "pool",
            "eng-team.dev",
            f"2026-11-22T12:{n:02d}:00+00:00",
            "x" * 15000,
        )
    page = status(kernel, "mail/text", limit=50)
    assert page.status_code == 200 and len(page.content) < 262144
    assert page.json()["next"] is not None


def test_changes_show_open_and_merged_with_review_state(changes: Kernel) -> None:
    view = status(changes, "changes").json()
    assert view["totals"] == {"open": 2, "merged": 1}
    assert [c["number"] for c in view["items"]] == [3, 2, 1]
    three, two, one = view["items"]
    assert (one["state"], one["review_state"], one["merged_by"]) == (
        "merged",
        "APPROVED",
        "eng-team.dev.0",
    )
    assert one["merge_sha"] == "deadbeef" and one["merged_at"] == "2026-11-21T12:00:00+00:00"
    # The newest deciding review wins over the earlier request for changes.
    assert (two["state"], two["review_state"]) == ("open", "APPROVED")
    assert [r["state"] for r in two["reviews"]] == ["APPROVED", "CHANGES_REQUESTED"]
    assert two["reviews"][0]["reviewer"] == "eng-team.principal.0"
    assert two["title"] == "Fix cache" and two["opened_by"] == "eng-team.dev.0"
    # A comment is not a decision.
    assert (three["state"], three["review_state"]) == ("open", "none")
    assert three["merged_at"] is None and three["url"].endswith("/pull/3")


def test_changes_filter_by_state_and_page(changes: Kernel) -> None:
    assert [c["number"] for c in status(changes, "changes", state="open").json()["items"]] == [3, 2]
    merged = status(changes, "changes", state="merged").json()
    assert [c["number"] for c in merged["items"]] == [1] and merged["totals"]["open"] == 2
    first = status(changes, "changes", limit=2).json()
    assert [c["number"] for c in first["items"]] == [3, 2] and first["next"]
    second = status(changes, "changes", limit=2, after=first["next"]).json()
    assert [c["number"] for c in second["items"]] == [1] and second["next"] is None
    paged = status(changes, "changes", state="open", limit=1).json()
    rest = status(changes, "changes", state="open", limit=1, after=paged["next"]).json()
    assert [c["number"] for c in (*paged["items"], *rest["items"])] == [3, 2]
    assert status(changes, "changes", after="garbage").status_code == 400
    assert status(changes, "changes", state="closed").status_code == 422


def test_changes_review_list_is_bounded(changes: Kernel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(team_status, "MAX_REVIEWS", 1)
    two = next(c for c in status(changes, "changes").json()["items"] if c["number"] == 2)
    assert [r["state"] for r in two["reviews"]] == ["APPROVED"] and two[
        "review_state"
    ] == "APPROVED"


def test_spend_shows_each_ceiling_against_use(setup: tuple[Kernel, Any, Any]) -> None:
    kernel, _, _ = setup
    document = example_group("trading-desk").model_dump(mode="json", by_alias=True)
    document["spec"]["spend"] = {"usd": 50, "runs": 1, "window": "day"}
    pool = document["spec"]["pools"][0]["name"]
    document["spec"]["pools"][0]["spend"] = {"usd": 20, "window": "week"}
    configure(
        kernel,
        "desk",
        "sup-desk",
        example_collections("trading-desk"),
        AgentGroup.model_validate(document),
    )
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO host_budget_limits VALUES ('sup-desk','desk',10000,1048576,2)"
        )
    run = submit(kernel, "sup-desk", "desk", f"desk.{pool}.0", DESK_POOLS[pool][1])
    view = status(kernel, "spend", who="sup-desk", group="desk", namespace="desk").json()
    group, first = view["accounts"]
    assert group["account"] == "group" and group["window"].startswith("day:UTC:")
    # The run reserved its `max_cost_usd` (2.0) against both accounts.
    assert group["usd"] == {"limit": 50, "used": 2.0, "remaining": 48.0}
    assert group["runs"] == {"limit": 1, "used": 1, "remaining": 0}
    assert group["at_ceiling"] is True
    assert first["account"] == f"pool:{pool}" and first["window"].startswith("week:UTC:")
    assert first["usd"] == {"limit": 20, "used": 2.0, "remaining": 18.0}
    assert first["runs"] is None and first["at_ceiling"] is False
    allocation = view["host_allocation"]
    assert allocation["tool_ms"] == {"limit": 10000, "used": 0, "remaining": 10000}
    assert allocation["concurrency"]["limit"] == 2 and allocation["at_ceiling"] is False
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO host_reservations VALUES (?,'sup-desk','desk',10000,0,0,0)", (run,)
        )
    exhausted = status(kernel, "spend", who="sup-desk", group="desk", namespace="desk").json()
    held = exhausted["host_allocation"]
    assert held["tool_ms"] == {"limit": 10000, "used": 10000, "remaining": 0}
    assert held["concurrency"]["used"] == 1 and held["at_ceiling"] is True


def test_spend_without_ceilings_is_empty(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    view = status(kernel, "spend").json()
    assert view["accounts"] == [] and view["host_allocation"] is None


def test_standing_reports_pauses_and_waivers(
    team: tuple[Kernel, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, _, _ = team
    view = status(kernel, "standing").json()
    assert view["group_state"] == "active" and view["paused"] is False
    assert view["members_disabled"] == [] and view["members_draining"] == []
    assert view["registry"] == "not_configured" and view["waived"] == []

    registry = tmp_path / "repositories.json"
    registry.write_text(
        json.dumps(
            {
                "repositories": {
                    "cardea": {
                        "source": "/srv/cardea.git",
                        "owner": "o",
                        "name": "n",
                        "api_url": "https://api.test",
                        "git_url": "https://git.test/o/n.git",
                        "host_rules": "waived",
                        "waiver_reason": "Free plan has no rulesets",
                    },
                    "other": {
                        "source": "/srv/other.git",
                        "owner": "o",
                        "name": "m",
                        "api_url": "https://api.test",
                        "git_url": "https://git.test/o/m.git",
                        "host_rules": "waived",
                        "waiver_reason": "Not this team's repository",
                    },
                }
            }
        )
    )
    registry.chmod(0o600)
    monkeypatch.setenv("CARDEA_REPOSITORIES", str(registry))
    view = status(kernel, "standing").json()
    # Only a repository the supervisor holds a `repo:` grant for is reported.
    assert view["registry"] == "ok"
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO policies VALUES ('sup-eng','eng','repo:cardea','allow',9999999999)"
        )
    view = status(kernel, "standing").json()
    assert view["waived"] == [{"repository": "cardea", "reason": "Free plan has no rulesets"}]
    registry.chmod(0o666)
    assert status(kernel, "standing").json()["registry"] == "invalid"

    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='eng-team.dev.1'")
    view = status(kernel, "standing").json()
    assert view["members_disabled"] == ["eng-team.dev.1"] and view["paused"] is False
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE principals SET disabled=1 WHERE principal_id IN "
            "(SELECT principal_id FROM group_members WHERE group_name='eng-team')"
        )
    view = status(kernel, "standing").json()
    assert view["paused"] is True and len(view["members_disabled"]) == 6
    work = status(kernel, "work")
    assert work.status_code == 200


@pytest.mark.parametrize("section", READABLE)
def test_only_the_groups_live_supervisor_may_read(
    team: tuple[Kernel, str, str], section: str
) -> None:
    kernel, _, _ = team
    assert status(kernel, section).status_code == 200
    assert status(kernel, section, who=None).status_code == 401
    assert status(kernel, section, who="nobody").status_code == 401
    other = status(kernel, section, who="sup-desk")
    assert other.status_code == 404 and other.json()["code"] == "group_not_found"
    assert status(kernel, section, namespace="desk").status_code == 404
    assert status(kernel, section, group="no-such-team").status_code == 404
    # An agent member is not the supervisor either.
    assert status(kernel, section, who="runtime").status_code == 404
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id='sup-eng'")
    gone = status(kernel, section)
    assert gone.status_code == 403 and gone.json()["code"] == "principal_disabled"


@pytest.mark.parametrize("section", READABLE)
def test_there_is_no_write_path(team: tuple[Kernel, str, str], section: str) -> None:
    kernel, _, _ = team
    client = TestClient(create_app(kernel))
    headers = {"Authorization": "Bearer sup-eng", "X-Cardea-Auth": "spiffe"}
    before = status(kernel, "work").json()["by_state"]
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(
            f"/v0.2/groups/eng-team/status/{section}", params={"namespace": "eng"}, headers=headers
        )
        assert response.status_code == 405, (method, section)
    assert status(kernel, "work").json()["by_state"] == before
    routed: list[set[str]] = [
        getattr(route, "methods", set())
        for route in create_app(kernel).routes
        if getattr(route, "path", "").startswith("/v0.2/groups/{name}/status/")
    ]
    assert len(routed) == len(READABLE) + 2 and all(methods == {"GET"} for methods in routed)


def plant_memory(kernel: Kernel, member: str, files: dict[str, str]) -> None:
    """A saved memory archive, as `cardea-memory save` would leave it."""
    import io
    import tarfile

    from cardea.kernel.models import ResourceRef
    from cardea.service.database import borrow
    from cardea.service.models import now

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1_700_000_000
            archive.addfile(info, io.BytesIO(data))
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        tx.put_object(
            ResourceRef(namespace="eng", resource_id=f"memory.{member}"),
            buffer.getvalue(),
            expected_version=0,
            producer_id=member,
            at=now(),
        )


def test_member_shows_grants_and_memory(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    member = "eng-team.dev.0"
    empty = status(kernel, f"members/{member}").json()
    assert empty["member"] == member and empty["pool"] == "dev" and not empty["disabled"]
    assert empty["memory"]["version"] is None and empty["memory"]["files"] == []
    assert empty["memory"]["limit_bytes"] == 1048576
    plant_memory(kernel, member, {"INDEX.md": "# Notes <b>x</b>", "notes/a.md": "hello"})
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO policies(principal_id,namespace,operation,mode,expires_at) "
            "VALUES (?,?,?,?,?)",
            (member, "eng", "mail:eng-team.lead", "allow", 4_102_444_800),
        )
        conn.exec_driver_sql(
            "INSERT INTO policies(principal_id,namespace,operation,mode,expires_at) "
            "VALUES (?,?,?,?,?)",
            (member, "eng", "mail:old", "allow", 1),
        )
    view = status(kernel, f"members/{member}").json()
    assert view["memory"]["version"] == 1 and view["memory"]["archive_bytes"] > 0
    assert [f["name"] for f in view["memory"]["files"]] == ["INDEX.md", "notes/a.md"]
    assert view["memory"]["files"][0]["text"] == "# Notes <b>x</b>"  # text, never markup
    operations = [c["operation"] for c in view["capabilities"]]
    assert "mail:eng-team.lead" in operations and "mail:old" not in operations
    # Another member's memory is not this member's.
    other = status(kernel, "members/eng-team.lead.0").json()
    assert other["memory"]["files"] == []


def test_member_refuses_a_stranger_and_a_non_supervisor(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    stranger = status(kernel, "members/eng-team.dev.9")
    assert stranger.status_code == 404 and stranger.json()["code"] == "member_not_found"
    assert status(kernel, "members/eng-team.dev.0", who="sup-desk").status_code == 404
    assert status(kernel, "members/eng-team.dev.0", who=None).status_code == 401


def test_runs_filter_by_member(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    items = status(kernel, "runs", member="eng-team.lead.0").json()["items"]
    assert [i["member"] for i in items] == ["eng-team.lead.0"]


def plant_call(kernel: Kernel, task_id: str, operation_id: str, snapshot: dict[str, Any]) -> None:
    with kernel.database.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO operations VALUES (?,?,?,?,?,?)",
            (
                task_id,
                operation_id,
                f"rcpt-{operation_id}",
                snapshot["state"],
                1,
                json.dumps(snapshot),
            ),
        )


def test_calls_list_a_runs_receipts_newest_first_and_page(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    task = a_task(kernel)
    for n in range(1, 4):
        plant_call(
            kernel,
            task,
            f"call-000{n}",
            {
                "state": "failed" if n == 2 else "succeeded",
                "created_at": f"2026-11-24T10:0{n}:00+00:00",
                "updated_at": f"2026-11-24T10:0{n}:01+00:00",
                "request": {"name": "env.try", "arguments": {"repo": "cardea", "n": n}},
                "result": {"output": "x" * 5000},
                "failure": {"code": "denied", "message": "no"} if n == 2 else None,
            },
        )
    plant_call(
        kernel,
        task,
        "other-1",
        {"state": "succeeded", "created_at": "x", "updated_at": "x", "request": {"name": "x"}},
    )
    page = status(kernel, "calls", task_id=task, limit=2).json()
    assert page["total"] == 3 and [i["operation_id"] for i in page["items"]] == [
        "call-0003",
        "call-0002",
    ]
    assert page["items"][1]["failure_code"] == "denied" and page["items"][0]["tool"] == "env.try"
    assert len(page["items"][0]["result"]) <= 2000
    rest = status(kernel, "calls", task_id=task, limit=2, after=page["next"]).json()
    assert [i["operation_id"] for i in rest["items"]] == ["call-0001"] and rest["next"] is None
    assert status(kernel, "calls", task_id="task-" + "0" * 32).status_code == 404
    assert status(kernel, "calls", task_id=task, who="sup-desk").status_code == 404
