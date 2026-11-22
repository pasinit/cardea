"""The host file's `team` section (deploy/team-kit/teamschema.py): the schema accepts a
configured team, refuses unknown or inconsistent fields naming them, and the plan-time check
reports a declared trigger with no grant. The schema module holds no role names."""

import copy
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

KIT = Path(__file__).resolve().parents[4] / "deploy" / "team-kit"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = load("teamschema")


def team() -> dict[str, Any]:
    return {
        "roles": {
            "scout": {
                "count": 2,
                "instructions": "Look around and report.",
                "grants": ["mailbox.inbox", "record:notes/read"],
                "triggers": [
                    {"schedule": {"cron": "0 9 * * 1-5"}},
                    {"event": {"types": ["cardea.mailbox.sent"]}},
                    {
                        "event": {
                            "types": ["cardea.record.created"],
                            "filters": [{"prefix": {"subject": "notes/"}}],
                        }
                    },
                ],
                "budget": {"runs": 4, "usd": 2.0},
            },
            "judge": {"instructions": "Decide.", "grants": []},
        },
        "budget": {"runs": 10, "usd": 5},
    }


def test_a_configured_team_parses_and_has_no_problems() -> None:
    roles = schema.parse(team())
    assert set(roles) == {"scout", "judge"}
    assert roles["scout"]["count"] == 2 and roles["judge"]["count"] == 1
    assert schema.grant_problems(roles) == []


def refused(mutate: Any, field: str) -> None:
    document = copy.deepcopy(team())
    mutate(document)
    with pytest.raises(schema.SchemaError) as error:
        schema.parse(document)
    assert field in str(error.value)


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda d: d.update(extra=1), "team.extra"),
        (lambda d: d["roles"]["scout"].update(replicas=1), "team.roles.scout.replicas"),
        (lambda d: d["roles"].update({"Bad Name": d["roles"]["judge"]}), "Bad Name"),
        (lambda d: d["roles"].clear(), "team.roles"),
        (lambda d: d["roles"]["scout"].pop("instructions"), "team.roles.scout.instructions"),
        (lambda d: d["roles"]["scout"].pop("grants"), "team.roles.scout.grants"),
        (lambda d: d["roles"]["scout"].update(count=-1), "team.roles.scout.count"),
        (lambda d: d["roles"]["scout"].update(count=True), "team.roles.scout.count"),
        (lambda d: d["roles"]["scout"].update(count=1.5), "team.roles.scout.count"),
        (lambda d: d["roles"]["scout"].update(instructions="  "), "scout.instructions"),
        (lambda d: d["roles"]["scout"].update(instructions="a\x00b"), "scout.instructions"),
        (lambda d: d["roles"]["scout"].update(instructions="x" * 20000), "scout.instructions"),
        (lambda d: d["roles"]["scout"].update(grants="all"), "team.roles.scout.grants"),
        (lambda d: d["roles"]["scout"]["grants"].append("mailbox.inbox"), "grants[2]"),
        (lambda d: d["roles"]["scout"]["grants"].append("bad grant"), "grants[2]"),
        (lambda d: d["roles"]["scout"]["triggers"].append({}), "triggers[3]"),
        (lambda d: d["roles"]["scout"]["triggers"][0].update(event={}), "triggers[0]"),
        (lambda d: d["roles"]["scout"]["triggers"][0]["schedule"].update(cron="daily"), "cron"),
        (lambda d: d["roles"]["scout"]["triggers"][1]["event"].update(types=["x.y"]), "types[0]"),
        (lambda d: d["roles"]["scout"]["triggers"][1]["event"].update(types=[]), "event.types"),
        (lambda d: d["roles"]["scout"]["triggers"][1]["event"].update(when=1), "event.when"),
        (lambda d: d["roles"]["scout"]["budget"].update(tokens=1), "budget.tokens"),
        (lambda d: d["roles"]["scout"]["budget"].update(runs=0), "budget.runs"),
        (lambda d: d["roles"]["scout"]["budget"].update(runs=1.5), "budget.runs"),
        (lambda d: d["roles"]["scout"].update(budget={}), "scout.budget"),
        (lambda d: d["budget"].update(runs=3), "team.budget.runs"),
        (lambda d: d["roles"]["scout"].update(count=0), "triggers cannot wake"),
    ],
)
def test_unknown_or_inconsistent_fields_are_refused_by_name(mutate: Any, field: str) -> None:
    refused(mutate, field)


def test_a_trigger_without_its_grant_is_reported() -> None:
    document = team()
    document["roles"]["scout"]["grants"] = []
    found = schema.grant_problems(schema.parse(document))
    assert len(found) == 2
    assert any("triggers[1]" in m and "'mailbox.inbox'" in m for m in found)
    assert any("triggers[2]" in m and "'record:notes/read'" in m for m in found)
    # A schedule needs no grant, and a wildcard grant covers the collection.
    document["roles"]["scout"]["grants"] = ["mailbox.inbox", "record:*"]
    assert schema.grant_problems(schema.parse(document)) == []


def test_a_record_event_without_a_collection_filter_is_reported() -> None:
    document = team()
    document["roles"]["scout"]["triggers"] = [{"event": {"types": ["cardea.record.created"]}}]
    (found,) = schema.grant_problems(schema.parse(document))
    assert "subject filter" in found and "triggers[0]" in found


def test_the_kit_loads_the_section_and_reports_through_plan(tmp_path: Path) -> None:
    kit = load("teamkit")
    setup = load("teamsetup")
    document = team()
    document["roles"]["scout"]["grants"] = ["mailbox.inbox"]
    host = {"namespace": "n", "supervisor": {"principal_id": "s"}, "repository": "r"}
    host["team"] = document
    path = tmp_path / "host.json"
    path.write_text(__import__("json").dumps(host))
    loaded = kit.load_host(path)
    (found,) = kit.team_problems(loaded)
    assert "record:notes/read" in found
    assert hasattr(setup, "TeamHost")
    host["team"]["roles"]["scout"]["count"] = "two"
    path.write_text(__import__("json").dumps(host))
    with pytest.raises(kit.HostError, match=r"team\.roles\.scout\.count"):
        kit.load_host(path)
    del host["team"]
    path.write_text(__import__("json").dumps(host))
    assert kit.team_problems(kit.load_host(path)) == []


def test_the_schema_code_holds_no_role_names() -> None:
    source = (KIT / "teamschema.py").read_text()
    words = set(re.findall(r"[a-z]+", source.lower()))
    for role in ("pm", "tl", "dev", "pe", "developer", "manager", "lead", "engineer"):
        assert role not in words, role
