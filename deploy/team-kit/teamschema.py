"""The `team` section of the host file: roles, per-role counts, triggers, grants, budgets and
instruction text (ADR-034).

The schema is generic: it knows no role by name. A host file declares whatever roles it wants

    "team": {"roles": {"<role>": {"count": 2, "instructions": "...", "grants": ["..."],
                                   "triggers": [{"kind": "mail", "grant": "mailbox.inbox"}],
                                   "budget": {"runs": 4, "usd": 1.0}}}}

and `validate` refuses what it does not understand, naming the field. `trigger_problems` is the
plan-time check: a trigger names the grant it needs, and the role must hold it.
"""

import re
from typing import Any

MAX_ROLES = 32
MAX_COUNT = 64
MAX_INSTRUCTIONS_BYTES = 16384
MAX_GRANTS = 128
MAX_TRIGGERS = 32
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_GRANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/*-]{0,127}")
_CRON = re.compile(r"[0-9*,/\- ]{1,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,64}")

SECTION_FIELDS = {"roles"}
ROLE_FIELDS = {"count", "instructions", "grants", "triggers", "budget"}
BUDGET_FIELDS = {"runs", "usd"}
# Each trigger kind's own fields, besides `kind` and `grant`.
TRIGGER_FIELDS: dict[str, set[str]] = {
    "schedule": {"cron"},
    "mail": set(),
    "created": set(),
    "state": {"state", "transition"},
}


class SchemaError(ValueError):
    """A field of the team section is unknown or inconsistent; the message names it."""


def _object(value: object, path: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{path} must be an object")
    for key in value:
        if key not in allowed:
            raise SchemaError(f"{path}.{key} is not a known field (known: {sorted(allowed)})")
    return value


def _integer(value: object, path: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise SchemaError(f"{path} must be a whole number from 1 to {high}")
    return value


def _text(value: object, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{path} must be non-empty text")
    if _CONTROL.search(value):
        raise SchemaError(f"{path} may not contain control characters")
    if len(value.encode()) > limit:
        raise SchemaError(f"{path} is longer than {limit} bytes")
    return value


def _budget(value: object, path: str) -> None:
    budget = _object(value, path, BUDGET_FIELDS)
    if not budget:
        raise SchemaError(f"{path} needs runs or usd")
    if "runs" in budget:
        _integer(budget["runs"], f"{path}.runs", 1_000_000)
    if "usd" in budget:
        usd = budget["usd"]
        if isinstance(usd, bool) or not isinstance(usd, int | float) or not 0 < usd <= 1e6:
            raise SchemaError(f"{path}.usd must be a positive number of US dollars")


def _grants(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_GRANTS:
        raise SchemaError(f"{path} must be a list of at most {MAX_GRANTS} grants")
    seen: set[str] = set()
    for index, grant in enumerate(value):
        if not isinstance(grant, str) or not _GRANT.fullmatch(grant):
            raise SchemaError(f"{path}[{index}] must be a grant name")
        if grant in seen:
            raise SchemaError(f"{path}[{index}] repeats the grant {grant!r}")
        seen.add(grant)
    return value


def _trigger(value: object, path: str) -> None:
    if not isinstance(value, dict):
        raise SchemaError(f"{path} must be an object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in TRIGGER_FIELDS:
        raise SchemaError(f"{path}.kind must be one of {sorted(TRIGGER_FIELDS)}")
    _object(value, path, {"kind", "grant", *TRIGGER_FIELDS[kind]})
    grant = value.get("grant")
    if not isinstance(grant, str) or not _GRANT.fullmatch(grant):
        raise SchemaError(f"{path}.grant must name the grant the trigger needs")
    if kind == "schedule":
        cron = value.get("cron")
        if not isinstance(cron, str) or not _CRON.fullmatch(cron) or len(cron.split()) != 5:
            raise SchemaError(f"{path}.cron must be a five-field cron expression")
    if kind == "state":
        if "state" not in value and "transition" not in value:
            raise SchemaError(f"{path} needs state or transition")
        for key in ("state", "transition"):
            named = value.get(key)
            if key in value and (not isinstance(named, str) or not _SLUG.fullmatch(named)):
                raise SchemaError(f"{path}.{key} must be a name")


def _role(value: object, path: str) -> None:
    role = _object(value, path, ROLE_FIELDS)
    for required in ("instructions", "grants"):
        if required not in role:
            raise SchemaError(f"{path}.{required} is required")
    if "count" in role:
        _integer(role["count"], f"{path}.count", MAX_COUNT)
    _text(role["instructions"], f"{path}.instructions", MAX_INSTRUCTIONS_BYTES)
    _grants(role["grants"], f"{path}.grants")
    triggers = role.get("triggers", [])
    if not isinstance(triggers, list) or len(triggers) > MAX_TRIGGERS:
        raise SchemaError(f"{path}.triggers must be a list of at most {MAX_TRIGGERS} triggers")
    for index, trigger in enumerate(triggers):
        _trigger(trigger, f"{path}.triggers[{index}]")
    if "budget" in role:
        _budget(role["budget"], f"{path}.budget")


def validate(host: dict[str, Any]) -> None:
    """Refuses an unknown or inconsistent field of the host file's `team` section, naming it.
    A host file without the section is fine."""
    if "team" not in host:
        return
    section = _object(host["team"], "team", SECTION_FIELDS)
    roles = section.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise SchemaError("team.roles must be an object naming at least one role")
    if len(roles) > MAX_ROLES:
        raise SchemaError(f"team.roles has more than {MAX_ROLES} roles")
    for name, role in roles.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SchemaError(f"team.roles.{name} is not a role name (lowercase, digits, -)")
        _role(role, f"team.roles.{name}")
    budgets = host.get("budgets")
    team = budgets.get("team", {}) if isinstance(budgets, dict) else {}
    for key in ("runs", "usd"):
        limit = team.get(key) if isinstance(team, dict) else None
        if not isinstance(limit, int | float) or isinstance(limit, bool):
            continue
        for name, role in roles.items():
            share = role.get("budget", {}).get(key)
            if share is not None and share > limit:
                raise SchemaError(
                    f"team.roles.{name}.budget.{key} ({share}) exceeds budgets.team.{key} ({limit})"
                )


def trigger_problems(host: dict[str, Any]) -> list[str]:
    """The plan-time check: each declared trigger whose grant its role does not hold."""
    problems: list[str] = []
    roles = (host.get("team") or {}).get("roles") or {}
    for name, role in roles.items():
        held = set(role.get("grants", []))
        for index, trigger in enumerate(role.get("triggers", [])):
            grant = trigger.get("grant")
            if grant not in held:
                problems.append(
                    f"team.roles.{name}.triggers[{index}] ({trigger.get('kind')}) "
                    f"needs the grant {grant!r}, which team.roles.{name}.grants lacks"
                )
    return problems
