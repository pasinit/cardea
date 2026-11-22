"""The team kit's host file schema (deploy/team-kit/teamschema.py): unknown or inconsistent
fields are refused naming the field, a trigger lacking its grant is reported at plan time, and
the schema code itself names no role."""

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

KIT = Path(__file__).resolve().parents[4] / "deploy" / "team-kit"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def role() -> dict[str, Any]:
    return {
        "count": 2,
        "instructions": "Do the work.",
        "grants": ["mailbox.inbox", "records.read"],
        "triggers": [
            {"kind": "mail", "grant": "mailbox.inbox"},
            {"kind": "schedule", "cron": "0 9 * * 1-5", "grant": "records.read"},
            {"kind": "state", "state": "ready", "grant": "records.read"},
        ],
        "budget": {"runs": 4, "usd": 1.5},
    }


def host(**changes: Any) -> dict[str, Any]:
    return {"team": {"roles": {"worker": {**role(), **changes}}}}


def example() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((KIT / "team.host.example.json").read_text()))


def test_a_valid_team_section_and_an_absent_one_are_accepted() -> None:
    schema = load("teamschema")
    schema.validate(host())
    schema.validate(example())
    assert schema.trigger_problems(host()) == []


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"colour": "red"}, "team.roles.worker.colour"),
        ({"count": 0}, "team.roles.worker.count"),
        ({"count": True}, "team.roles.worker.count"),
        ({"count": "2"}, "team.roles.worker.count"),
        ({"instructions": "  "}, "team.roles.worker.instructions"),
        ({"instructions": "a\x00b"}, "team.roles.worker.instructions"),
        ({"grants": ["a", "a"]}, "team.roles.worker.grants[1]"),
        ({"grants": "all"}, "team.roles.worker.grants"),
        ({"triggers": [{"kind": "tide"}]}, "team.roles.worker.triggers[0].kind"),
        (
            {"triggers": [{"kind": "mail", "grant": "mailbox.inbox", "x": 1}]},
            "team.roles.worker.triggers[0].x",
        ),
        ({"triggers": [{"kind": "mail"}]}, "team.roles.worker.triggers[0].grant"),
        (
            {"triggers": [{"kind": "schedule", "grant": "records.read", "cron": "soon"}]},
            "team.roles.worker.triggers[0].cron",
        ),
        (
            {"triggers": [{"kind": "state", "grant": "records.read"}]},
            "team.roles.worker.triggers[0]",
        ),
        ({"budget": {}}, "team.roles.worker.budget"),
        ({"budget": {"tokens": 3}}, "team.roles.worker.budget.tokens"),
        ({"budget": {"usd": -1}}, "team.roles.worker.budget.usd"),
    ],
)
def test_bad_fields_are_refused_naming_the_field(changes: dict[str, Any], field: str) -> None:
    schema = load("teamschema")
    with pytest.raises(schema.SchemaError, match=re.escape(field)):
        schema.validate(host(**changes))


def test_missing_and_unknown_sections_are_refused() -> None:
    schema = load("teamschema")
    with pytest.raises(schema.SchemaError, match=r"team\.roles"):
        schema.validate({"team": {}})
    with pytest.raises(schema.SchemaError, match=r"team\.extra"):
        schema.validate({"team": {"roles": {"a": role()}, "extra": 1}})
    missing = role()
    del missing["grants"]
    with pytest.raises(schema.SchemaError, match=r"team\.roles\.a\.grants"):
        schema.validate({"team": {"roles": {"a": missing}}})
    with pytest.raises(schema.SchemaError, match=r"team\.roles\.Bad"):
        schema.validate({"team": {"roles": {"Bad": role()}}})


def test_a_role_budget_above_the_team_budget_is_inconsistent() -> None:
    schema = load("teamschema")
    document = host()
    document["budgets"] = {"team": {"runs": 3, "usd": 20.0}}
    with pytest.raises(schema.SchemaError, match=r"team\.roles\.worker\.budget\.runs.*budgets"):
        schema.validate(document)


def test_a_trigger_lacking_its_grant_is_reported_at_plan_time() -> None:
    schema = load("teamschema")
    document = host(grants=["records.read"])
    problems: list[str] = schema.trigger_problems(document)
    assert len(problems) == 1
    assert "team.roles.worker.triggers[0]" in problems[0]
    assert "'mailbox.inbox'" in problems[0]


def test_load_host_refuses_and_the_kit_reports_problems(tmp_path: Path) -> None:
    kit = load("teamkit")
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**example(), **host(colour="red")}))
    with pytest.raises(kit.HostError, match=r"team\.roles\.worker\.colour"):
        kit.load_host(path)
    path.write_text(json.dumps({**example(), **host(grants=[])}))
    loaded = kit.load_host(path)
    found: list[str] = kit.team_problems(loaded)
    assert len(found) == 3
    assert "triggers[2]" in found[2]


def test_plan_reports_a_trigger_lacking_its_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    kit = load("teamkit")
    problems: list[str] = kit.team_problems(host(grants=["records.read"]))
    assert [p for p in problems if "mailbox.inbox" in p]
    setup = load("teamsetup")
    lines: list[str] = []
    out: Callable[[str], None] = lines.append
    ctx = cast(Any, type("Ctx", (), {"kit": kit, "host": host(grants=[])})())
    monkeypatch.setattr(setup, "steps", lambda: [])
    assert setup.plan(ctx, out) is False
    assert len(lines) == 3 and all(line.startswith("team: ") for line in lines)


def test_schema_code_contains_no_role_names() -> None:
    names = {*load("teamkit").ROLE_NAMES, *load("teamkit").ROLE_NAMES.values()}
    source = (KIT / "teamschema.py").read_text().lower()
    for name in names:
        assert not re.search(rf"\b{re.escape(name)}\b", source), name
