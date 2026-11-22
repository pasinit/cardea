"""The team settings model: the host file's operator-facing settings, validated one by one, and
a preview of what a change would alter (no apply, no UI).

A settings document names only the settings to change:

    {"run_limits": {"team": 30, "roles": {"dev": 14}},
     "spend_limits": {"team_usd": 20.0, "per_run_usd": 1.0, "roles_usd": {"dev": 5.0}},
     "paused": {"paused": true, "reason": "release freeze"},
     "schedules": {"pm": ["0 9 * * 1-5"]},
     "developers": 3,
     "model": "claude-sonnet-4-5",
     "product_goals": {"goals": "...", "non_goals": "..."}}

`validate` refuses a setting it does not understand with an error naming the field. `preview`
reads the host file at a path, applies the change to a copy and returns the differences; it
writes nothing. Pause and resume are read against the pause record the kit keeps beside the
provisioning file, since the host file does not hold them.

Authority is not decided here. This module holds no principal and caches no permission: whether
the supervisor may change a setting is checked live by the kernel when a change is applied, and
a preview says so. Secrets (credentials, tokens, the kernel socket) are outside the settings: a
settings document naming one is refused, and a preview never reads or prints them.
"""

import copy
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

POOLS = ("pm", "tl", "dev", "pe")
MAX_RUNS = 1_000_000
MAX_USD = 1e6
MAX_DEVELOPERS = 64
MAX_SCHEDULES = 32
MAX_MODEL = 128
MAX_REASON = 200
MAX_PRODUCT_BYTES = 8192
_CRON = re.compile(r"[0-9*,/\- ]{1,64}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # newlines and tabs allowed

SETTINGS = (
    "run_limits",
    "spend_limits",
    "paused",
    "schedules",
    "developers",
    "model",
    "product_goals",
)
# Fields a settings document may never carry: they hold or point at secrets.
SECRET_FIELDS = ("credentials", "token", "secret", "password", "kernel", "supervisor")
REDACTED = "[redacted]"
AUTHORITY_NOTE = (
    "authority is not checked by a preview: the kernel checks the supervisor's live "
    "authority when a change is applied"
)


class SettingsError(ValueError):
    """A setting is unknown or invalid; the message names the field."""


def _object(value: object, path: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError(f"{path} must be an object")
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise SettingsError(f"{path}.{key} is not a known field (known: {sorted(allowed)})")
    return value


def _runs(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RUNS:
        raise SettingsError(f"{path} must be a whole number from 1 to {MAX_RUNS}")
    return value


def _usd(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= MAX_USD:
        raise SettingsError(f"{path} must be a positive number of US dollars")
    return float(value)


def _text(value: object, path: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{path} must be non-empty text")
    if (_TEXT_CONTROL if multiline else _CONTROL).search(value):
        raise SettingsError(f"{path} may not contain control characters")
    if len(value.encode()) > limit:
        raise SettingsError(f"{path} is longer than {limit} bytes")
    return value


def _per_role(value: object, path: str, check: Any) -> dict[str, Any]:
    roles = _object(value, path, POOLS)
    if not roles:
        raise SettingsError(f"{path} needs at least one role")
    return {role: check(v, f"{path}.{role}") for role, v in roles.items()}


def _run_limits(value: object, path: str) -> dict[str, Any]:
    found = _object(value, path, ("team", "roles"))
    if not found:
        raise SettingsError(f"{path} needs team or roles")
    out: dict[str, Any] = {}
    if "team" in found:
        out["team"] = _runs(found["team"], f"{path}.team")
    if "roles" in found:
        out["roles"] = _per_role(found["roles"], f"{path}.roles", _runs)
    return out


def _spend_limits(value: object, path: str) -> dict[str, Any]:
    found = _object(value, path, ("team_usd", "per_run_usd", "roles_usd"))
    if not found:
        raise SettingsError(f"{path} needs team_usd, per_run_usd or roles_usd")
    out: dict[str, Any] = {}
    for key in ("team_usd", "per_run_usd"):
        if key in found:
            out[key] = _usd(found[key], f"{path}.{key}")
    if "roles_usd" in found:
        out["roles_usd"] = _per_role(found["roles_usd"], f"{path}.roles_usd", _usd)
    return out


def _paused(value: object, path: str) -> dict[str, Any]:
    found = _object(value, path, ("paused", "reason"))
    if not isinstance(found.get("paused"), bool):
        raise SettingsError(f"{path}.paused must be true (pause) or false (resume)")
    out: dict[str, Any] = {"paused": found["paused"]}
    if "reason" in found:
        if not found["paused"]:
            raise SettingsError(f"{path}.reason applies only when pausing")
        out["reason"] = _text(found["reason"], f"{path}.reason", MAX_REASON)
    return out


def _schedules(value: object, path: str) -> dict[str, list[str]]:
    def one(crons: object, where: str) -> list[str]:
        if not isinstance(crons, list) or len(crons) > MAX_SCHEDULES:
            raise SettingsError(f"{where} must be a list of at most {MAX_SCHEDULES} schedules")
        seen: set[str] = set()
        for index, cron in enumerate(crons):
            if not isinstance(cron, str) or not _CRON.fullmatch(cron) or len(cron.split()) != 5:
                raise SettingsError(f"{where}[{index}] must be a five-field cron expression")
            if cron in seen:
                raise SettingsError(f"{where}[{index}] repeats the schedule {cron!r}")
            seen.add(cron)
        return crons

    found = _object(value, path, POOLS)
    if not found:
        raise SettingsError(f"{path} needs at least one role")
    return {role: one(crons, f"{path}.{role}") for role, crons in found.items()}


def _developers(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DEVELOPERS:
        raise SettingsError(f"{path} must be a whole number from 1 to {MAX_DEVELOPERS}")
    return value


def _model(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_MODEL:
        raise SettingsError(f"{path} must be a model name of at most {MAX_MODEL} characters")
    if not _MODEL.fullmatch(value):
        raise SettingsError(f"{path} must be a model name (letters, digits and . _ : / @ -)")
    return value


def _product_goals(value: object, path: str) -> dict[str, str]:
    found = _object(value, path, ("goals", "non_goals"))
    if "goals" not in found:
        raise SettingsError(f"{path}.goals is required")
    out = {k: _text(v, f"{path}.{k}", MAX_PRODUCT_BYTES, multiline=True) for k, v in found.items()}
    if sum(len(v.encode()) for v in out.values()) > MAX_PRODUCT_BYTES:
        raise SettingsError(f"{path} goals and non_goals are over {MAX_PRODUCT_BYTES} bytes")
    return {k: v.strip() for k, v in out.items()}


_CHECKS = {
    "run_limits": _run_limits,
    "spend_limits": _spend_limits,
    "paused": _paused,
    "schedules": _schedules,
    "developers": _developers,
    "model": _model,
    "product_goals": _product_goals,
}


def validate(settings: object) -> dict[str, Any]:
    """The settings document, checked setting by setting and returned normalised. An unknown
    setting, a secret-bearing field or an invalid value raises `SettingsError` naming the field.
    The error never echoes the offending value, so a secret pasted into a field is not shown."""
    if not isinstance(settings, dict) or not settings:
        raise SettingsError("settings must be an object naming at least one setting")
    for key in settings:
        if isinstance(key, str) and key.lower() in SECRET_FIELDS:
            raise SettingsError(f"{key} is not a setting: secrets are never set here")
    found = _object(settings, "settings", SETTINGS)
    return {key: _CHECKS[key](value, key) for key, value in found.items()}


def _kit() -> ModuleType:
    """The sibling team kit, loaded by path like the kit loads its schema."""
    spec = importlib.util.spec_from_file_location(
        "teamkit_for_settings", Path(__file__).with_name("teamkit.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("teamkit_for_settings", module)
    spec.loader.exec_module(module)
    return sys.modules["teamkit_for_settings"]


def current(host: dict[str, Any], kit: ModuleType, *, paused: bool) -> dict[str, Any]:
    """The settings as the host file and the pause record hold them now, defaults filled in
    as the kit applies them. Nothing secret is read."""
    budgets = {**kit.DEFAULT_BUDGETS, **(host.get("budgets") or {})}
    roles = {**kit.DEFAULT_BUDGETS["roles"], **(budgets.get("roles") or {})}
    team = budgets.get("team") or {}
    schedules = {**kit.DEFAULT_SCHEDULES, **(host.get("schedules") or {})}
    shown = kit.product(host) or {}
    return {
        "run_limits": {
            "team": team.get("runs"),
            "roles": {r: roles.get(r, {}).get("runs") for r in POOLS},
        },
        "spend_limits": {
            "team_usd": team.get("usd"),
            "per_run_usd": budgets.get("max_cost_usd_per_run", 1.0),
            "roles_usd": {r: roles.get(r, {}).get("usd") for r in POOLS},
        },
        "paused": {"paused": paused},
        "schedules": {r: list(schedules.get(r, [])) for r in POOLS},
        "developers": int(host.get("developers", 2)),
        "model": host.get("model"),
        "product_goals": {k: shown[k] for k in ("goals", "non_goals") if k in shown},
    }


def _put(host: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = host
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[path[-1]] = value


def apply_to(host: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """A copy of the host document with validated settings applied: the shape the host file
    would have. Used by `preview`; nothing is written."""
    out = copy.deepcopy(host)
    runs = settings.get("run_limits", {})
    if "team" in runs:
        _put(out, ("budgets", "team", "runs"), runs["team"])
    for role, value in runs.get("roles", {}).items():
        _put(out, ("budgets", "roles", role, "runs"), value)
    spend = settings.get("spend_limits", {})
    if "team_usd" in spend:
        _put(out, ("budgets", "team", "usd"), spend["team_usd"])
    if "per_run_usd" in spend:
        _put(out, ("budgets", "max_cost_usd_per_run"), spend["per_run_usd"])
    for role, value in spend.get("roles_usd", {}).items():
        _put(out, ("budgets", "roles", role, "usd"), value)
    for role, crons in settings.get("schedules", {}).items():
        _put(out, ("schedules", role), crons)
    if "developers" in settings:
        out["developers"] = settings["developers"]
    if "model" in settings:
        out["model"] = settings["model"]
    if "product_goals" in settings:
        # An inline value replaces a goals_file: the section holds exactly one of them.
        out["product"] = dict(settings["product_goals"])
    return out


def _leaves(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for key, child in value.items():
            out.update(_leaves(child, f"{path}.{key}" if path else str(key)))
        return out
    return {path: value}


def _budget_problems(after: dict[str, Any]) -> list[str]:
    """Role shares above the team ceiling, the schema's own rule, found before an apply."""
    problems = []
    team = after.get("team") or {}
    for key, label in (("runs", "runs"), ("usd", "usd")):
        ceiling = team.get(key)
        for role, share in (after.get("roles") or {}).items():
            value = (share or {}).get(key)
            if ceiling is not None and value is not None and value > ceiling:
                problems.append(
                    f"budgets.roles.{role}.{label} ({value}) exceeds budgets.team.{label} "
                    f"({ceiling})"
                )
    return problems


def preview(host_path: Path, settings: object, *, paused: bool | None = None) -> dict[str, Any]:
    """What applying `settings` would change, against the host file at `host_path`.

    Returns `{"changes": [{"setting", "field", "from", "to"}], "problems": [...],
    "unchanged": [...], "authority": ...}`. `problems` are findings that would stop an apply
    (the host file's own validation of the changed document). Nothing is written, no kernel is
    called and no credential is read. `paused` is the team's present pause state; by default it
    is read from the kit's pause record beside the provisioning file."""
    wanted = validate(settings)
    kit = _kit()
    host = kit.load_host(host_path)
    if paused is None:
        paused = bool(kit.read_pause_record(host))
    before = current(host, kit, paused=paused)
    changed = apply_to(host, wanted)
    problems: list[str] = []
    try:
        kit._schema().validate(changed)
    except ValueError as error:
        problems.append(str(error))
    problems += _budget_problems(changed.get("budgets") or {})
    if "product_goals" in wanted and "product" in host and "goals_file" in host["product"]:
        problems.append("product.goals_file is replaced by inline goals")
    after = copy.deepcopy(before)
    for setting, value in wanted.items():
        replaces = setting == "product_goals" or not isinstance(value, dict)
        after[setting] = value if replaces else _merge(before[setting], value)
    old, new = _leaves(before), _leaves(after)
    named = _leaves(wanted)
    changes = []
    unchanged = []
    for field in sorted(set(old) | set(new)):
        setting = field.split(".", 1)[0]
        if setting not in wanted or (setting != "product_goals" and field not in named):
            continue
        if old.get(field) != new.get(field):
            changes.append(
                {"setting": setting, "field": field, "from": old.get(field), "to": new.get(field)}
            )
        else:
            unchanged.append(field)
    return {
        "changes": changes,
        "unchanged": unchanged,
        "problems": problems,
        "authority": AUTHORITY_NOTE,
    }


def _merge(before: Any, value: dict[str, Any]) -> Any:
    """`before` with the nested `value` laid over it. Schedules and goals replace whole."""
    if not isinstance(before, dict):
        return value
    out = dict(before)
    for key, child in value.items():
        if isinstance(child, dict) and isinstance(before.get(key), dict):
            out[key] = _merge(before[key], child)
        else:
            out[key] = child
    return out


def render(result: dict[str, Any]) -> list[str]:
    """A preview as plain lines. Goals text is shown by its size, not its content."""
    lines = []
    for change in result["changes"]:
        old, new = change["from"], change["to"]
        if change["setting"] == "product_goals":
            old, new = _size(old), _size(new)
        lines.append(f"{change['field']}: {old!r} -> {new!r}")
    if not result["changes"]:
        lines.append("no change")
    lines += [f"problem: {p}" for p in result["problems"]]
    lines.append(result["authority"])
    return lines


def _size(value: Any) -> str:
    return f"{len(value.encode())} bytes" if isinstance(value, str) else "unset"
