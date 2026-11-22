"""The host file's `team` section: roles, counts, triggers, grants, budgets, instructions.

A team is configuration, not code: this module knows the shape of a role and the rules that
tie its triggers to its grants, and nothing about any particular role. Role names are keys the
host owner chooses.

    "team": {
      "roles": {
        "<role>": {
          "count": 1,
          "instructions": "text the role's runs start from",
          "grants": ["mailbox.inbox", "record:<collection>/read"],
          "triggers": [
            {"schedule": {"cron": "0 9 * * 1-5"}},
            {"event": {"types": ["cardea.mailbox.sent"]}}
          ],
          "budget": {"runs": 4, "usd": 2.0}
        }
      }
    }

`parse` refuses an unknown or inconsistent field with an error naming it (`team.roles.<role>
.count`). `grant_problems` is the plan-time check: a trigger whose event the role holds no
grant to read is reported, since the run it would start could not see what woke it.
"""

import re
from typing import Any

MAX_INSTRUCTIONS_BYTES = 16384
MAX_COUNT = 64
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
GRANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/*@-]{0,127}")
CRON_FIELDS = 5
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

ROLE_KEYS = {"count", "instructions", "grants", "triggers", "budget"}
BUDGET_KEYS = {"runs", "usd"}
# What a trigger's event lets a run read, as the grant that must be held. `{collection}` is
# taken from the trigger's subject filter. These are kernel event types, not team roles.
EVENT_GRANTS: dict[str, str] = {
    "cardea.mailbox.sent": "mailbox.inbox",
    "cardea.record.created": "record:{collection}/read",
    "cardea.record.transitioned": "record:{collection}/read",
}


class SchemaError(ValueError):
    """A field of the team section is unknown or inconsistent; the message names it."""


def _object(value: object, path: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{path} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SchemaError(f"{path}.{unknown[0]} is not a known field (allowed: {sorted(allowed)})")
    return value


def _text(value: object, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{path} must be non-empty text")
    if _CONTROL.search(value):
        raise SchemaError(f"{path} may not contain control characters")
    if len(value.encode()) > limit:
        raise SchemaError(f"{path} is over {limit} bytes")
    return value.strip()


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise SchemaError(f"{path} must be a positive number")
    return float(value)


def _grants(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise SchemaError(f"{path} must be a list of grants")
    seen: set[str] = set()
    for index, grant in enumerate(value):
        if not isinstance(grant, str) or not GRANT.fullmatch(grant):
            raise SchemaError(f"{path}[{index}] is not a grant name")
        if grant in seen:
            raise SchemaError(f"{path}[{index}] repeats {grant!r}")
        seen.add(grant)
    return list(value)


def _trigger(value: object, path: str) -> dict[str, Any]:
    trigger = _object(value, path, {"schedule", "event"})
    if len(trigger) != 1:
        raise SchemaError(f"{path} needs exactly one of schedule and event")
    if "schedule" in trigger:
        schedule = _object(trigger["schedule"], f"{path}.schedule", {"cron"})
        cron = schedule.get("cron")
        if not isinstance(cron, str) or len(cron.split()) != CRON_FIELDS:
            raise SchemaError(f"{path}.schedule.cron must be a {CRON_FIELDS}-field cron line")
        return trigger
    event = _object(trigger["event"], f"{path}.event", {"types", "filters"})
    types = event.get("types")
    if not isinstance(types, list) or not types:
        raise SchemaError(f"{path}.event.types must be a non-empty list")
    for index, kind in enumerate(types):
        if kind not in EVENT_GRANTS:
            raise SchemaError(
                f"{path}.event.types[{index}] {kind!r} is not a known event type "
                f"(known: {sorted(EVENT_GRANTS)})"
            )
    filters = event.get("filters", [])
    if not isinstance(filters, list) or not all(isinstance(f, dict) for f in filters):
        raise SchemaError(f"{path}.event.filters must be a list of objects")
    return trigger


def _role(value: object, path: str) -> dict[str, Any]:
    role = _object(value, path, ROLE_KEYS)
    for key in ("instructions", "grants"):
        if key not in role:
            raise SchemaError(f"{path}.{key} is required")
    count = role.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_COUNT:
        raise SchemaError(f"{path}.count must be a whole number from 0 to {MAX_COUNT}")
    parsed: dict[str, Any] = {
        "count": count,
        "instructions": _text(role["instructions"], f"{path}.instructions", MAX_INSTRUCTIONS_BYTES),
        "grants": _grants(role["grants"], f"{path}.grants"),
        "triggers": [],
    }
    triggers = role.get("triggers", [])
    if not isinstance(triggers, list):
        raise SchemaError(f"{path}.triggers must be a list")
    parsed["triggers"] = [_trigger(t, f"{path}.triggers[{i}]") for i, t in enumerate(triggers)]
    if "budget" in role:
        budget = _object(role["budget"], f"{path}.budget", BUDGET_KEYS)
        if not budget:
            raise SchemaError(f"{path}.budget needs runs or usd")
        parsed["budget"] = {k: _number(v, f"{path}.budget.{k}") for k, v in budget.items()}
        if "runs" in budget and parsed["budget"]["runs"] != int(parsed["budget"]["runs"]):
            raise SchemaError(f"{path}.budget.runs must be a whole number")
    if count == 0 and parsed["triggers"]:
        raise SchemaError(f"{path}.triggers cannot wake a role whose count is 0")
    return parsed


def parse(team: object, path: str = "team") -> dict[str, dict[str, Any]]:
    """The team's roles, validated, keyed by role name. Raises SchemaError naming the field."""
    section = _object(team, path, {"roles", "budget"})
    roles = section.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise SchemaError(f"{path}.roles must name at least one role")
    parsed = {}
    for name, value in roles.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise SchemaError(f"{path}.roles key {name!r} must be lowercase letters, digits, -")
        parsed[name] = _role(value, f"{path}.roles.{name}")
    if "budget" in section:
        team_budget = _object(section["budget"], f"{path}.budget", BUDGET_KEYS)
        if not team_budget:
            raise SchemaError(f"{path}.budget needs runs or usd")
        limits = {k: _number(v, f"{path}.budget.{k}") for k, v in team_budget.items()}
        for key, limit in limits.items():
            total = sum(r["budget"].get(key, 0) for r in parsed.values() if "budget" in r)
            if total > limit:
                raise SchemaError(
                    f"{path}.budget.{key} is {limit:g}, below the roles' budgets ({total:g})"
                )
    return parsed


def _collections(trigger: dict[str, Any]) -> list[str]:
    """The record collections named by the event's subject filters (`prefix`/`exact`)."""
    found = []
    for item in trigger["event"].get("filters", []):
        for kind in ("prefix", "exact"):
            subject = (item.get(kind) or {}).get("subject")
            if isinstance(subject, str) and "/" in subject:
                found.append(subject.split("/", 1)[0])
    return found


def _held(grants: list[str], wanted: str) -> bool:
    return any(g == wanted or (g.endswith("*") and wanted.startswith(g[:-1])) for g in grants)


def grant_problems(roles: dict[str, dict[str, Any]], path: str = "team") -> list[str]:
    """Plan-time check: each event trigger needs the grant that lets its run read the event.
    One message per missing grant, naming the trigger and the grant to add."""
    found = []
    for name, role in roles.items():
        for index, trigger in enumerate(role["triggers"]):
            if "event" not in trigger:
                continue
            where = f"{path}.roles.{name}.triggers[{index}]"
            for kind in trigger["event"]["types"]:
                template = EVENT_GRANTS[kind]
                if "{collection}" not in template:
                    needed = [template]
                else:
                    collections = _collections(trigger)
                    if not collections:
                        found.append(
                            f"{where}: {kind} needs a subject filter naming the collection, "
                            f"so the grant {template.replace('{collection}', '<collection>')!r} "
                            "can be checked"
                        )
                        continue
                    needed = [template.format(collection=c) for c in collections]
                for grant in needed:
                    if not _held(role["grants"], grant):
                        found.append(
                            f"{where}: {kind} has no grant; add {grant!r} to "
                            f"{path}.roles.{name}.grants"
                        )
    return found
