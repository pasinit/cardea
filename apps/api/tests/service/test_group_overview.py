"""A supervisor's group overview (ADR-034 amendment, 2026-11-16), on the worked examples."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from test_worked_examples import (
    DESK_POOLS,
    ENG_POOLS,
    act,
    configure,
    example_collections,
    example_group,
    setup,
    tool,
)

from cardea.service import group_overview
from cardea.service.app import create_app
from cardea.service.kernel import Kernel
from cardea.service.models import AgentGroup

__all__ = ["setup"]


def overview(kernel: Kernel, who: str = "sup-eng", **params: Any) -> Any:
    client = TestClient(create_app(kernel))
    return client.get(
        f"/v0.2/groups/{params.pop('group', 'eng-team')}/overview",
        params={"namespace": params.pop("namespace", "eng"), **params},
        headers={"Authorization": f"Bearer {who}", "X-Cardea-Auth": "spiffe"},
    )


def eng_step(kernel: Kernel, session: Any, member: str, operations: list[str], script: Any) -> Any:
    state, result = act(
        kernel,
        session,
        namespace="eng",
        supervisor="sup-eng",
        member=member,
        grants=ENG_POOLS[member.split(".")[1]][1],
        operations=operations,
        script=script,
    )
    assert state == "succeeded", (member, result)
    return result


def file_work(key: str) -> Any:
    async def script(client: ClientSession) -> Any:
        return await tool(
            client, "records-create", collection="work", client_key=key, data={"title": key}
        )

    return script


def transition(record_id: str, name: str, **extra: Any) -> Any:
    async def script(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-transition",
            collection="work",
            record_id=record_id,
            transition=name,
            **extra,
        )

    return script


@pytest.fixture
def team(setup: tuple[Kernel, Any, Any]) -> tuple[Kernel, str, str]:
    """The software team part-way through its work: one item claimed, one open, mail sent."""
    kernel, session, _ = setup
    group = example_group("software-team")
    configure(kernel, "eng", "sup-eng", example_collections("software-team"), group)
    first = eng_step(kernel, session, "eng-team.product.0", ["records.create"], file_work("a"))
    second = eng_step(kernel, session, "eng-team.product.0", ["records.create"], file_work("b"))
    item = first["record"]["record_id"]

    async def assign(client: ClientSession) -> Any:
        await tool(
            client,
            "records-update",
            collection="work",
            record_id=item,
            expected_version=1,
            data={"title": "a", "assignee_pool": "eng-team.dev"},
        )
        await tool(
            client,
            "mailbox-send",
            client_key="heads-up",
            to={"pool": "eng-team.dev"},
            parts=[{"text": "Item a is ready."}],
        )
        return await transition(item, "triage", args={"assignee_pool": "eng-team.dev"})(client)

    eng_step(
        kernel,
        session,
        "eng-team.lead.0",
        ["records.update", "records.transition", "mailbox.send"],
        assign,
    )
    eng_step(kernel, session, "eng-team.dev.0", ["records.transition"], transition(item, "start"))
    return kernel, item, second["record"]["record_id"]


@pytest.mark.loopback
def test_the_overview_shows_members_work_mail_and_journal(team: tuple[Kernel, str, str]) -> None:
    kernel, item, _ = team
    response = overview(kernel)
    assert response.status_code == 200, response.json()
    view = response.json()
    assert (view["group"], view["namespace"], view["state"]) == ("eng-team", "eng", "active")
    assert view["observed_generation"] == view["generation"] and view["as_of"]
    pools = {p["name"]: p for p in view["pools"]}
    assert set(pools) == {"principal", "product", "lead", "dev"}
    assert [m["principal_id"] for m in pools["dev"]["members"]] == [
        "eng-team.dev.0",
        "eng-team.dev.1",
        "eng-team.dev.2",
    ]
    dev0 = pools["dev"]["members"][0]
    assert dev0["claims"] == [f"work/{item}"] and dev0["claims_truncated"] == 0
    assert dev0["last_run"]["outcome"] == "succeeded" and dev0["active_task_id"] is None
    assert pools["principal"]["members"][0]["last_run"] is None
    work, decisions = view["collections"]
    assert work["name"] == "work" and work["status"] == "ok"
    assert work["by_state"] == {"in_progress": 1, "open": 1} and work["total"] == 2
    (claim,) = work["claimed"]
    assert claim["record_id"] == item and claim["holder"] == "eng-team.dev.0"
    assert claim["claim_epoch"] == 1 and claim["age_seconds"] is not None
    assert decisions == {
        "name": "decisions",
        "status": "ok",
        "by_state": {},
        "total": 0,
        "claimed": [],
        "truncated": 0,
    }
    assert view["mailbox"]["addresses"] == [
        {"address": "eng-team.dev", "pending": 1, "in_flight": 0, "dead": 0, "consumed": 0}
    ]
    assert view["approvals"] == {"items": [], "truncated": 0}
    assert view["spend"] == {"accounts": [], "expectations": [], "expectations_truncated": 0}
    types = [e["type"] for e in view["journal"]["events"]]
    assert "cardea.record.transitioned" in types and "cardea.mailbox.sent" in types
    cursors = [e["cursor"] for e in view["journal"]["events"]]
    assert cursors == sorted(cursors, reverse=True) and cursors[0] <= view["cursor"]


@pytest.mark.loopback
def test_every_list_is_bounded_and_says_what_it_left_out(
    team: tuple[Kernel, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel, _, _ = team
    whole = overview(kernel).json()["journal"]
    one = overview(kernel, events=1).json()["journal"]
    assert len(one["events"]) == 1
    assert one["truncated"] == len(whole["events"]) + whole["truncated"] - 1 > 0
    assert overview(kernel, events=101).status_code == 422
    monkeypatch.setattr(group_overview, "MAX_CLAIMED_PER_COLLECTION", 0)
    monkeypatch.setattr(group_overview, "MAX_CLAIMS_PER_MEMBER", 0)
    monkeypatch.setattr(group_overview, "MAX_ADDRESSES", 0)
    view = overview(kernel).json()
    work = view["collections"][0]
    assert work["claimed"] == [] and work["truncated"] == 1
    dev0 = next(p for p in view["pools"] if p["name"] == "dev")["members"][0]
    assert dev0["claims"] == [] and dev0["claims_truncated"] == 1
    assert view["mailbox"] == {"addresses": [], "truncated": 1}


@pytest.mark.loopback
def test_only_the_groups_supervisor_may_read_it(team: tuple[Kernel, str, str]) -> None:
    kernel, _, _ = team
    refused = overview(kernel, who="sup-desk")
    assert refused.status_code == 404 and refused.json()["code"] == "group_not_found"
    assert overview(kernel, namespace="desk").status_code == 404
    assert overview(kernel, who="nobody").status_code == 401


@pytest.mark.loopback
def test_spend_and_open_approvals_appear_for_the_desk(setup: tuple[Kernel, Any, Any]) -> None:
    kernel, session, _ = setup
    document = example_group("trading-desk").model_dump(mode="json", by_alias=True)
    document["spec"]["spend"] = {"usd": 50, "window": "day"}
    configure(
        kernel,
        "desk",
        "sup-desk",
        example_collections("trading-desk"),
        AgentGroup.model_validate(document),
    )

    async def propose(client: ClientSession) -> Any:
        return await tool(
            client,
            "records-create",
            collection="orders",
            client_key="o-1",
            data={
                "instrument": "XYZ",
                "side": "buy",
                "qty": 1,
                "limit_price": 9.0,
                "rationale": "Test",
            },
        )

    _, made = act(
        kernel,
        session,
        namespace="desk",
        supervisor="sup-desk",
        member="desk.analyst.0",
        grants=DESK_POOLS["analyst"][1],
        operations=["records.create"],
        script=propose,
    )
    order = made["record"]["record_id"]

    async def approve(client: ClientSession) -> Any:
        return await tool(
            client, "records-transition", collection="orders", record_id=order, transition="approve"
        )

    state, _ = act(
        kernel,
        session,
        namespace="desk",
        supervisor="sup-desk",
        member="desk.risk.0",
        grants=DESK_POOLS["risk"][1],
        operations=["records.transition"],
        script=approve,
    )
    assert state == "waiting"
    view = overview(kernel, who="sup-desk", group="desk", namespace="desk").json()
    (account,) = view["spend"]["accounts"]
    assert account["account"] == "group" and account["usd"] == 50
    assert account["window"].startswith("day:UTC:") and account["used_usd"] > 0
    (held,) = view["approvals"]["items"]
    assert held["state"] == "pending" and held["resource"] == "record:orders/approve"
    risk = next(p for p in view["pools"] if p["name"] == "risk")["members"][0]
    assert risk["active_task_id"] == held["task_id"]
