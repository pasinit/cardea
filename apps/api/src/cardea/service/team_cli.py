"""`cardea team`: run the dev team (deploy/team-kit) from the installed CLI.

The supervisor's SVID is bound to the `cardea-kernel` account, so the commands run as it. A
plain user never types `sudo -u`: the front end (`main`) parses and validates the command,
asks the questions (confirm a settings change, a stop) and sends one JSON request to
`/usr/libexec/cardea-team-run` through the package's narrow sudoers rule (`cardea-operators`
group, that one program, no arguments). The helper (`helper_main`) is the only code that runs
as `cardea-kernel`:

- it reads the host file from the fixed path `/etc/cardea/team.host.json`, never from the
  request, and refuses one that is not a regular file owned by root or itself and writable by
  no one else, so a caller cannot make it present the supervisor's SVID to a host of their
  choosing or read other files;
- it runs the team kit that ships in this package, with this package's code (`provision` is
  the package's own `main.py`), the same version as the running kernel;
- it decides nothing about authority. Every read and write goes to the kernel as the
  supervisor, which checks the supervisor's live authority; a refusal is reported by its code;
- it returns text only: no credential, no SVID, and no lower layer's error message.

Run directly as `cardea-kernel` (or in tests), `--host` selects another host file.
"""

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

KERNEL_USER = "cardea-kernel"
HELPER = "/usr/libexec/cardea-team-run"
SUDO = "/usr/bin/sudo"
DEFAULT_HOST = Path("/etc/cardea/team.host.json")
MAX_REQUEST = 65536
MAX_TEXT = 4000
MAX_REASON = 200
MAIL_TARGETS = ("tl", "pm", "pe", "ux", "dev.N")
ROLE_WORDS = {
    "pm": "product manager",
    "tl": "team lead",
    "dev": "developers",
    "pe": "principal engineer",
    "ux": "UX designer",
}
ROLES = ("pm", "tl", "dev", "pe")
SET_KEYS = """\
runs.team N | runs.<role> N        runs a day for the team or one role
usd.team X | usd.run X | usd.<role> X   dollars a day for the team, one run or one role
schedule.<role> "CRON[; CRON...]"  five-field cron schedules; "none" clears them
email.rate COUNT/DAYS              the email rate limit, for example 10/1
developers N                       how many developers
model NAME                         the model
goals TEXT | goals @FILE           the product goals
non-goals TEXT | non-goals @FILE   what the team must not build
(<role> is pm, tl, dev or pe)"""

_TARGET = re.compile(r"^(?:(tl|pm|pe|ux)|dev\.(0|[1-9][0-9]{0,2}))$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ITEM = re.compile(r"^[a-z][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MEMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CURSOR = re.compile(r"^[0-9a-f-]{36}$")


class TeamError(Exception):
    """A refusal with a plain message. Messages hold no secret and no lower layer's text."""


def kit_dir() -> Path:
    """The team kit shipped in this package (`/usr/lib/cardea/team-kit`), or the checkout's
    `deploy/team-kit` when run from a source tree."""
    here = Path(__file__).resolve()
    for found in (here.parents[2] / "team-kit", here.parents[5] / "deploy" / "team-kit"):
        if (found / "teamkit.py").is_file():
            return found
    raise TeamError("the team kit is not installed")


def kit_modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    """The kit, its settings model, its settings apply and its dashboard backend, loaded by
    path from `kit_dir()` (the kit is a directory of scripts, not a package)."""
    import importlib.util

    directory = kit_dir()
    apply_path = directory / "teamsettings_apply.py"
    spec = importlib.util.spec_from_file_location("teamsettings_apply_cli", apply_path)
    assert spec is not None and spec.loader is not None
    applying = sys.modules.get("teamsettings_apply_cli")
    if applying is None:
        applying = importlib.util.module_from_spec(spec)
        sys.modules["teamsettings_apply_cli"] = applying
        spec.loader.exec_module(applying)
    kit, model = applying._modules()
    board = sys.modules.get("teamdashboard_cli")
    if board is None:
        dash = importlib.util.spec_from_file_location(
            "teamdashboard_cli", directory / "dashboard.py"
        )
        assert dash is not None and dash.loader is not None
        board = importlib.util.module_from_spec(dash)
        sys.modules["teamdashboard_cli"] = board
        dash.loader.exec_module(board)
    return kit, model, applying, board


# Settings keys -> settings documents (the dashboard's model validates them)


def _whole(key: str, value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,9}", value.strip()):
        raise TeamError(f"{key} needs a whole number")
    return int(value)


def _dollars(key: str, value: str) -> float:
    try:
        return float(value.strip().lstrip("$"))
    except ValueError:
        raise TeamError(f"{key} needs a number of US dollars") from None


def settings_for(key: str, value: str, current_goals: str | None = None) -> dict[str, Any]:
    """The settings document for `set <key> <value>`. Only the shape is made here; the
    settings model validates every value, with the same rules as the dashboard's page."""
    head, _, tail = key.partition(".")
    if key == "developers":
        return {"developers": _whole(key, value)}
    if key == "model":
        return {"model": value.strip()}
    if key in ("goals", "non-goals"):
        if key == "goals":
            return {"product_goals": {"goals": value}}
        if current_goals is None:
            raise TeamError("there are no goals yet: set goals first")
        return {"product_goals": {"goals": current_goals, "non_goals": value}}
    if head == "runs" and tail == "team":
        return {"run_limits": {"team": _whole(key, value)}}
    if head == "runs" and tail in ROLES:
        return {"run_limits": {"roles": {tail: _whole(key, value)}}}
    if head == "usd" and tail == "team":
        return {"spend_limits": {"team_usd": _dollars(key, value)}}
    if head == "usd" and tail == "run":
        return {"spend_limits": {"per_run_usd": _dollars(key, value)}}
    if head == "usd" and tail in ROLES:
        return {"spend_limits": {"roles_usd": {tail: _dollars(key, value)}}}
    if head == "schedule" and tail in ROLES:
        crons = [] if value.strip().lower() in ("", "none") else value.split(";")
        return {"schedules": {tail: [c.strip() for c in crons]}}
    if key == "email.rate":
        count, slash, days = value.partition("/")
        if not slash:
            raise TeamError("email.rate needs COUNT/DAYS, for example 10/1")
        return {
            "email_rate_limit": {
                "count": _whole("email.rate count", count),
                "window_days": _whole("email.rate days", days),
            }
        }
    raise TeamError(f"{key!r} is not a setting key; the keys are:\n{SET_KEYS}")


# The helper: runs as cardea-kernel


def check_host_file(path: Path) -> None:
    """Refuse a host file the caller could have written or swapped: it must be a regular
    file, not a link, owned by root or the running account, and writable by no one else."""
    try:
        info = path.lstat()
    except OSError:
        raise TeamError(f"the host file {path} cannot be read") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o022
    ):
        raise TeamError(
            f"the host file {path} must be a regular file owned by root or {KERNEL_USER} "
            "and writable by no one else"
        )


def _failure(error: Exception) -> str:
    code = getattr(error, "code", None)
    return type(error).__name__ + (f" ({code})" if isinstance(code, str) else "")


def _say(host: dict[str, Any], kit: ModuleType) -> tuple[str, str]:
    return str(host["namespace"]), str(kit._names(host)[1])


def describe(host: dict[str, Any], kit: ModuleType, model: ModuleType) -> list[str]:
    """The host settings in plain words. Nothing secret is read or shown."""
    paused = bool(kit.read_pause_record(host))
    now = model.current(host, kit, paused=paused)
    namespace, group = _say(host, kit)
    runs, spend, crons = now["run_limits"], now["spend_limits"], now["schedules"]
    lines = [
        f"Team {group} in {namespace}: " + ("PAUSED" if paused else "not paused"),
        f"Developers: {now['developers']}",
        f"Model: {now['model']}",
        "Runs a day: team "
        + str(runs["team"])
        + "; "
        + ", ".join(f"{ROLE_WORDS[r]} {runs['roles'][r]}" for r in ROLES),
    ]
    per_role = [f"{ROLE_WORDS[r]} ${v:.2f}" for r, v in spend["roles_usd"].items() if v is not None]
    lines.append(
        f"Spend a day: team ${spend['team_usd']:.2f}; one run at most ${spend['per_run_usd']:.2f}"
        + ("; " + ", ".join(per_role) if per_role else "; no per-role dollar limits")
    )
    for role in ROLES:
        found = crons[role]
        lines.append(
            f"Schedule, {ROLE_WORDS[role]}: "
            + ("; ".join(found) if found else "none (wakes on events and mail only)")
        )
    email = now["email_rate_limit"]
    lines.append(
        "Email to the owner (product manager and UX designer): at most "
        f"{email['count']} every {email['window_days']} day(s)"
        if email
        else "Email to the owner: not configured"
    )
    goals = now["product_goals"]
    if goals.get("goals"):
        first = goals["goals"].strip().splitlines()[0][:100]
        size = len(goals["goals"].encode()) + len(goals.get("non_goals", "").encode())
        lines.append(f"Product goals: {size} bytes; they begin: {first}")
    else:
        lines.append("Product goals: none")
    lines.append("Change one with `cardea team set <key> <value>`; `cardea team set --help`.")
    return lines


def _mail_text(parts: Any) -> str:
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return "\n".join(texts)


def _send_mail(host: dict[str, Any], kit: ModuleType, target: str, text: str) -> dict[str, Any]:
    found = _TARGET.match(target)
    if not found:
        raise TeamError(f"the address must be one of {', '.join(MAIL_TARGETS)} (dev.0, dev.1...)")
    if not text.strip() or len(text) > MAX_TEXT:
        raise TeamError(f"the message must be 1 to {MAX_TEXT} characters")
    if _TEXT_CONTROL.search(text):
        raise TeamError("the message may not contain control characters")
    namespace, group = _say(host, kit)
    if found.group(2) is not None:
        if int(found.group(2)) >= int(host.get("developers", 2)):
            raise TeamError(f"there is no {target}: the team has {host.get('developers', 2)}")
        to: dict[str, str] = {"instance": f"{group}.{target}"}
    else:
        to = {"pool": f"{group}.{target}"}
    kernel = kit.client(host)
    try:
        sent: dict[str, Any] = kernel.request(
            "POST",
            "/mailbox",
            json={
                "namespace": namespace,
                "client_key": uuid.uuid4().hex,
                "to": to,
                "parts": [{"text": text}],
            },
        )
    finally:
        kernel.close()
    return {"message_id": sent.get("message_id")}


def _list_mail(
    host: dict[str, Any], kit: ModuleType, board: ModuleType, request: dict[str, Any]
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for key, pattern in (("member", _MEMBER), ("item", _ITEM), ("after", _CURSOR)):
        if request.get(key):
            if not isinstance(request[key], str) or not pattern.match(request[key]):
                raise TeamError(f"{key} is not valid")
            query[key] = request[key]
    limit = request.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise TeamError("limit must be a whole number from 1 to 50")
    query["limit"] = limit
    namespace, group = _say(host, kit)
    kernel = kit.client(host)
    try:
        page = board.KernelBackend(kernel.request, namespace, group).mail(query)
    finally:
        kernel.close()
    items = [
        {
            "message_id": m.get("message_id"),
            "sent_at": m.get("sent_at"),
            "from": (m.get("from") or {}).get("address"),
            "to": (m.get("to") or {}).get("address"),
            "item": m.get("item"),
            "text": _mail_text(m.get("parts")),
        }
        for m in page.get("items", [])
    ]
    return {"items": items, "total": page.get("total"), "next": page.get("next")}


def _render(host: dict[str, Any], kit: ModuleType) -> dict[str, Any]:
    warnings = [f"WARNING: {w}" for w in kit.wall_problems(host)]
    if kit.session_wall(host) is None:
        warnings.append("note: the session profile's wall time is unknown; check it")
    with tempfile.TemporaryDirectory() as scratch:
        files = {p.name: p.read_text() for p in kit.render(host, Path(scratch))}
    return {"files": files, "lines": warnings}


def execute(
    request: dict[str, Any],
    host_path: Path = DEFAULT_HOST,
    *,
    who: str | None = None,
) -> dict[str, Any]:
    """Run one request as the team's supervisor and return `{"ok", "lines", ...}`. Never
    raises: a failure is `{"ok": False, "error": <plain text>}`."""
    try:
        return _execute(request, host_path, who)
    except TeamError as error:
        return {"ok": False, "error": str(error)}
    except Exception as error:
        kit_error = _is_known(error)
        return {"ok": False, "error": kit_error or f"failed ({_failure(error)})"}


def _is_known(error: Exception) -> str | None:
    """Validation errors of the kit and the settings model name the field and never echo a
    value; they are the plain message. Every other error is reported by type and code only."""
    if type(error).__name__ in {"SettingsError", "HostError", "ApplyError"}:
        return f"refused: {error}"
    return None


def _execute(request: dict[str, Any], host_path: Path, who: str | None) -> dict[str, Any]:
    command = request.get("command")
    if command not in {
        "status",
        "pause",
        "resume",
        "stop",
        "apply",
        "render",
        "provision",
        "settings",
        "set",
        "mail",
        "mail_list",
    }:
        raise TeamError("unknown command")
    check_host_file(host_path)
    kit, model, applying, board = kit_modules()
    host = kit.load_host(host_path)
    if command == "status":
        return {"ok": True, "lines": kit.status(host)}
    if command == "pause":
        reason = request.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON * 4):
            raise TeamError("the reason must be short plain text")
        runs = kit.pause(host, reason)
        return {
            "ok": True,
            "lines": [
                f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)",
                *(f"  {run}" for run in runs),
            ],
        }
    if command == "resume":
        kit.resume(host)
        return {"ok": True, "lines": ["the supervisor is enabled; triggers admit again"]}
    if command == "stop":
        runs, problem = kit.stop(host)
        lines = [
            f"the group was not deleted ({problem}); delete it after `resume`"
            if problem
            else "group deleted",
            f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)",
        ]
        return {"ok": problem is None, "lines": lines, "error": "stop did not finish"}
    if command == "apply":
        return {"ok": True, "lines": kit.apply(host, kit.client)}
    if command == "provision":
        return {"ok": True, "lines": [str(kit.provision(host, paused=bool(request.get("paused"))))]}
    if command == "render":
        return {"ok": True, **_render(host, kit)}
    if command == "settings":
        return {"ok": True, "lines": describe(host, kit, model)}
    if command == "set":
        key, value = request.get("key"), request.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            raise TeamError("set needs a key and a value")
        goals = (kit.product(host) or {}).get("goals")
        settings = settings_for(key, value, goals)
        confirm = request.get("confirm")
        if confirm is not None and not isinstance(confirm, str):
            raise TeamError("the confirmation is not valid")
        sudo_user = os.environ.get("SUDO_USER")
        found = applying.apply_settings(
            host_path, settings, confirm=confirm, by=f"cli:{who or sudo_user or 'direct'}"
        )
        return {
            "ok": True,
            "lines": model.render(found["preview"]),
            "status": found["status"],
            "confirm": found["confirm"],
        }
    if command == "mail":
        sent = _send_mail(host, kit, str(request.get("target")), str(request.get("text")))
        return {"ok": True, "lines": [f"sent: message {sent['message_id']}"]}
    return {"ok": True, **_list_mail(host, kit, board, request)}


def helper_main() -> int:
    """`/usr/libexec/cardea-team-run`: one JSON request on stdin, one JSON response on stdout.
    Anything a child process prints goes to stderr, so stdout holds the response only."""
    reply = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        request = json.loads(raw) if len(raw) <= MAX_REQUEST else None
        if not isinstance(request, dict):
            response: dict[str, Any] = {"ok": False, "error": "the request is not valid"}
        elif os.geteuid() != _kernel_uid():
            response = {"ok": False, "error": f"the helper must run as {KERNEL_USER}"}
        else:
            response = execute(request, DEFAULT_HOST, who=os.environ.get("SUDO_USER"))
    except (ValueError, TeamError):
        response = {"ok": False, "error": "the request is not valid"}
    json.dump(response, reply)
    reply.write("\n")
    reply.flush()
    return 0


def _kernel_uid() -> int:
    try:
        return pwd.getpwnam(KERNEL_USER).pw_uid
    except KeyError:
        raise TeamError(f"the {KERNEL_USER} account does not exist") from None


# The front end: runs as the user


def plain(text: object) -> str:
    """Text for a terminal: control characters (escape sequences in mail from agents, for
    example) are replaced, newlines and tabs kept."""
    return _CONTROL.sub("?", str(text).replace("\r", "\n")) if text is not None else ""


def deliver(
    request: dict[str, Any],
    host: Path | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Send a request to where the supervisor identity lives. As `cardea-kernel` itself, run it
    in this process (and `host` may name another host file). Otherwise through the helper and
    its sudoers rule, with the fixed host file."""
    if os.geteuid() == _kernel_uid():
        return execute(request, host or DEFAULT_HOST)
    if host is not None:
        raise TeamError(f"--host is only for {KERNEL_USER}: the helper uses {DEFAULT_HOST}")
    done = runner(
        [SUDO, "-n", "-u", KERNEL_USER, HELPER],
        input=json.dumps(request).encode(),
        stdout=subprocess.PIPE,
        check=False,
    )
    try:
        response = json.loads(done.stdout)
    except ValueError:
        response = None
    if not isinstance(response, dict):
        raise TeamError(
            "cannot run as "
            f"{KERNEL_USER}: you must be in the cardea-operators group (log in again after "
            "being added), and the team host file must exist; see cardea-team(1)"
        )
    return response


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="cardea team",
        description="Run the agent dev team: status, pause and resume, settings, mail.",
        epilog="Commands run as the team's supervisor, kernel-checked; see cardea-team(1).",
    )
    commands = top.add_subparsers(dest="command", metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--host", type=Path, help=f"host file (only as {KERNEL_USER}; default {DEFAULT_HOST})"
    )

    def add(name: str, help_: str, *, yes: bool = False) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_, description=help_, parents=[common])
        if yes:
            sub.add_argument("--yes", "-y", action="store_true", help="do not ask to confirm")
        return sub

    add("status", "show the team: runs, work, mail, spend, credential expiry, failures")
    add("pause", "the kill switch: stop every run and admit nothing until `resume`").add_argument(
        "reason", nargs="*", help="why, kept for `status` (no secrets)"
    )
    add("resume", "enable the supervisor again; triggers admit from their next slot")
    add("stop", "pause, then delete the group (records stay); asks to confirm", yes=True)
    add("apply", "put the collections and the group to the kernel as the supervisor")
    add("render", "print the documents the host file renders, for review").add_argument(
        "--out", type=Path, help="write them to this directory instead of printing"
    )
    add("provision", "write the supervisor's grants and provision them").add_argument(
        "--paused", action="store_true", help="provision the supervisor disabled"
    )
    add("settings", "show the host settings in plain words")
    sub = add(
        "set",
        "change a setting: preview the diff, confirm, then apply",
        yes=True,
    )
    sub.formatter_class = argparse.RawDescriptionHelpFormatter
    sub.description = (
        "Change a setting: preview the diff, confirm, then apply.\n\nkeys:\n" + SET_KEYS
    )
    sub.add_argument("key")
    sub.add_argument("value", nargs="+")
    sub = add("mail", "send supervisor mail to a role (`mail tl|pm|pe|ux|dev.N TEXT`) or list it")
    sub.add_argument("target", metavar="tl|pm|pe|ux|dev.N|list")
    sub.add_argument("text", nargs="*", help="the message (`-` reads it from stdin)")
    sub.add_argument("--limit", type=int, help="with `list`: how many, 1 to 50 (default 20)")
    sub.add_argument("--member", help="with `list`: only mail to or from this principal")
    sub.add_argument("--item", help="with `list`: only mail about this record, COLLECTION/ID")
    return top


def main(
    argv: list[str],
    *,
    send: Callable[[dict[str, Any], Path | None], dict[str, Any]] = deliver,
    ask: Callable[[str], str] = input,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    stdin = stdin or sys.stdin

    def say(text: object, stream: TextIO = out) -> None:
        print(plain(text), file=stream)

    top = parser()
    try:
        args = top.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    if not args.command:
        top.print_help(out)
        return 2

    def call(request: dict[str, Any]) -> dict[str, Any]:
        found = send(request, args.host)
        for line in found.get("lines", []):
            say(line)
        if not found.get("ok"):
            say(f"cardea team: {found.get('error') or 'failed'}", err)
        return found

    def confirmed(question: str) -> bool:
        if args.yes:
            return True
        if not stdin.isatty():
            say("nothing changed: confirm with --yes, or run it from a terminal", err)
            return False
        return ask(question).strip().lower() in ("y", "yes")

    try:
        return _run(args, call, confirmed, stdin, say)
    except TeamError as error:
        say(f"cardea team: {error}", err)
        return 1


def _run(
    args: argparse.Namespace,
    call: Callable[[dict[str, Any]], dict[str, Any]],
    confirmed: Callable[[str], bool],
    stdin: TextIO,
    say: Callable[..., None],
) -> int:
    command = args.command
    if command in ("status", "resume", "apply", "settings"):
        return 0 if call({"command": command}).get("ok") else 1
    if command == "pause":
        reason = " ".join(args.reason) or None
        return 0 if call({"command": "pause", "reason": reason}).get("ok") else 1
    if command == "provision":
        return 0 if call({"command": "provision", "paused": args.paused}).get("ok") else 1
    if command == "stop":
        if not confirmed("Pause the team and delete its group? Records stay. [y/N] "):
            return 1
        return 0 if call({"command": "stop"}).get("ok") else 1
    if command == "render":
        found = call({"command": "render"})
        if not found.get("ok"):
            return 1
        for name, text in sorted(found["files"].items()):
            if Path(name).name != name or name in ("", ".", ".."):
                raise TeamError("the helper returned an unsafe file name")
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / name).write_text(text)
                say(args.out / name)
            else:
                say(f"== {name} ==\n{text}")
        return 0
    if command == "set":
        return _set(args, call, confirmed, say)
    return _mail(args, call, stdin, say)


def _set(
    args: argparse.Namespace,
    call: Callable[[dict[str, Any]], dict[str, Any]],
    confirmed: Callable[[str], bool],
    say: Callable[..., None],
) -> int:
    value = " ".join(args.value)
    if args.key in ("goals", "non-goals") and value.startswith("@"):
        try:
            value = Path(value[1:]).read_text()
        except (OSError, UnicodeDecodeError):
            raise TeamError(f"cannot read {value[1:]}") from None
    request = {"command": "set", "key": args.key, "value": value}
    found = call(request)
    if not found.get("ok"):
        return 1
    if found["status"] == "unchanged":
        say("already in effect; nothing changed")
        return 0
    if not confirmed("Apply this change? [y/N] "):
        say("nothing applied")
        return 1
    done = call({**request, "confirm": found["confirm"]})
    if not done.get("ok"):
        return 1
    if done["status"] != "applied":
        raise TeamError("the settings changed since the preview; nothing applied")
    say("applied")
    return 0


def _mail(
    args: argparse.Namespace,
    call: Callable[[dict[str, Any]], dict[str, Any]],
    stdin: TextIO,
    say: Callable[..., None],
) -> int:
    if args.target == "list":
        if args.text:
            raise TeamError("`mail list` takes no text")
        request: dict[str, Any] = {"command": "mail_list"}
        for key in ("limit", "member", "item"):
            if getattr(args, key) is not None:
                request[key] = getattr(args, key)
        found = call(request)
        if not found.get("ok"):
            return 1
        for item in found["items"]:
            about = item.get("item") or {}
            label = f" [{about.get('collection')}/{about.get('record_id')}]" if about else ""
            say(f"{item['sent_at']}  {item['from']} -> {item['to']}{label}")
            say("  " + plain(item["text"]).replace("\n", "\n  "))
        say(f"{len(found['items'])} of {found.get('total')} message(s)")
        return 0
    if args.limit is not None or args.member or args.item:
        raise TeamError("--limit, --member and --item go with `mail list`")
    text = " ".join(args.text)
    if text == "-":
        text = stdin.read(MAX_TEXT + 1)
    if not _TARGET.match(args.target):
        raise TeamError(f"the address must be one of {', '.join(MAIL_TARGETS)}, or `list`")
    return 0 if call({"command": "mail", "target": args.target, "text": text}).get("ok") else 1
