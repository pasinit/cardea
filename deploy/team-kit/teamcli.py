"""`cardea team`: the team kit's commands as a subcommand group of the installed `cardea`.

    cardea team status | pause [reason] | resume | stop | apply | render | provision
    cardea team settings
    cardea team set <key> <value>
    cardea team mail <tl|pm|pe|ux|dev.N> <text>
    cardea team mail list

A person types these on the host. The supervisor's identity (its SPIFFE SVID) is bound to the
`cardea-kernel` account, and so is the kernel's socket, so the work is done by that account.
Two halves, in this one file:

- The front end runs as the person. It parses the command line, asks for confirmation, writes
  `render`'s files where the person asks, and sends one JSON request to the helper by
  `sudo -n -u cardea-kernel /usr/libexec/cardea-team`. The sudoers rule names that one program
  with no arguments and only the `cardea-operators` group, so the request travels on stdin, and
  the helper checks every field again: it trusts nothing the front end sent.
- The helper runs as `cardea-kernel`. It reads the fixed host file (`DEFAULT_HOST`; a caller of
  the helper cannot name another, or it could make the account act under any identity the host
  file named), then does the work through the team kit (`teamkit`, `teamsettings`,
  `teamsettings_apply`, `dashboard`), which this package installs beside the kernel's own code.

Nothing here decides who may do what. Every read and write goes to the kernel as the supervisor,
and the kernel checks its live authority (the same checks as the dashboard's settings page and
message box); the group that may run the helper only decides who can reach the supervisor's
identity at all. Output holds no credential: the host file's credentials and kernel sections are
never printed, errors are an exception type and a kernel error code, and text that came from
the team (mail) has its terminal control characters replaced before it is shown.
"""

import argparse
import importlib.util
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
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_HOST = Path("/etc/cardea/team.host.json")
HELPER = "/usr/libexec/cardea-team"
KERNEL_USER = "cardea-kernel"
OPERATORS_GROUP = "cardea-operators"
SUDO = "/usr/bin/sudo"
MAX_REQUEST_BYTES = 64 * 1024
MAX_VALUE = 9000
MAX_REASON = 1000
MAX_SHOWN_TEXT = 4000
ROLES = ("tl", "pm", "pe", "ux")
DEVELOPER = re.compile(r"dev\.(0|[1-9][0-9]{0,2})")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_TEXT_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")

# Every command, as `--help` and the man page list it: (usage, what it does).
COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "the team's members, work, mail, spend and last failures, or why it is paused"),
    ("pause [reason]", "the kill switch: cancel every unfinished run and admit no more"),
    ("resume", "let the team run again after a pause"),
    ("stop", "pause, then delete the agent group (asks first; collections and records stay)"),
    ("apply", "put the work and decisions collections and the agent group to the kernel"),
    ("render [--out DIR]", "show the documents the team is made of, or write them to DIR"),
    ("provision [--paused]", "grant the team's supervisor its rights (--paused: disabled)"),
    ("settings", "the host settings in force, in plain words"),
    ("set <key> <value>", "change one setting: show the change, ask, then apply it"),
    (
        "mail <tl|pm|pe|ux|dev.N> <text>",
        "send a message to a role, or to developer N, as the supervisor",
    ),
    (
        "mail list [--member ID] [--item COLLECTION/ID] [--limit N] [--after CURSOR]",
        "read the team's mail",
    ),
)
# The keys `set` takes: (key, what it sets, an example value).
SET_KEYS: tuple[tuple[str, str, str], ...] = (
    ("runs.team", "runs a day for the whole team", "30"),
    ("runs.<role>", "runs a day for pm, tl, dev or pe", "14"),
    ("usd.team", "dollars a day for the whole team", "20"),
    ("usd.per_run", "dollars one run may cost at most", "1.5"),
    ("usd.<role>", "dollars a day for pm, tl, dev or pe", "5"),
    (
        "schedule.<role>",
        "cron schedules, separated by ';' ('none' for no schedule)",
        "0 9 * * 1-5; 0 15 * * 1-5",
    ),
    ("email.rate_limit", "emails to the client: COUNT/DAYS", "10/1"),
    ("developers", "how many developers", "3"),
    ("model", "the model name", "claude-sonnet-4-5"),
    ("goals", "the product's goals ('@FILE' or '-' reads them)", "@goals.txt"),
    ("non_goals", "the product's non-goals ('@FILE' or '-' reads them)", "@non-goals.txt"),
)
EPILOG = """\
Run as any user in the group `cardea-operators`: the commands act through the `cardea-kernel`
account, whose identity is the team supervisor's, and the kernel checks the supervisor's
authority on every call. The host file is /etc/cardea/team.host.json.
Put `--` before a message or value that starts with `-`. Details: man cardea-team."""


def help_text() -> str:
    lines = ["usage: cardea team <command> [arguments]", "", "commands:"]
    lines += [f"  {usage}\n      {what}" for usage, what in COMMANDS]
    lines += ["", "keys for `set`:"]
    lines += [f"  {key}\n      {what} (for example {example})" for key, what, example in SET_KEYS]
    lines += ["", "options of `set`, `stop`:", "  --yes      do not ask for confirmation"]
    lines += ["  --dry-run  (set) show the change and stop", "", EPILOG]
    return "\n".join(lines)


class Refusal(Exception):
    """A request the helper will not carry out; the message is safe to show."""


def clean(text: str, *, multiline: bool = True) -> str:
    """Text for a terminal: control characters (escape sequences, carriage returns, bidi
    overrides) replaced, newlines and tabs kept."""
    out = _UNSAFE.sub("�", text)
    return out if multiline else out.replace("\n", " ").replace("\t", " ")


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return sys.modules[name]


def modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    """The team kit, the settings model, the apply module and the dashboard backend."""
    applier = load("teamsettings_apply")
    kit, model = applier._modules()
    return kit, model, applier, load("dashboard")


# ---------------------------------------------------------------------------------------------
# The helper's side: a validated request, carried out as `cardea-kernel`.
# ---------------------------------------------------------------------------------------------


def _fields(request: dict[str, Any], required: dict[str, type], optional: dict[str, type]) -> None:
    allowed = {"command", *required, *optional}
    if set(request) - allowed or set(required) - set(request):
        raise Refusal("the request does not have the fields of that command")
    for name, kind in {**required, **optional}.items():
        if name in request and request[name] is not None:
            value = request[name]
            if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                raise Refusal(f"{name} has the wrong type")
            if isinstance(value, str) and len(value) > MAX_VALUE:
                raise Refusal(f"{name} is too long")


def check_request(request: object) -> dict[str, Any]:
    """The request, if it is exactly one of the commands with exactly its fields."""
    if not isinstance(request, dict) or not isinstance(request.get("command"), str):
        raise Refusal("the request is not a command")
    command = request["command"]
    nothing: dict[str, type] = {}
    shapes: dict[str, tuple[dict[str, type], dict[str, type]]] = {
        "status": (nothing, nothing),
        "pause": (nothing, {"reason": str}),
        "resume": (nothing, nothing),
        "stop": (nothing, nothing),
        "apply": (nothing, nothing),
        "render": (nothing, nothing),
        "provision": (nothing, {"paused": bool}),
        "settings": (nothing, nothing),
        "set": ({"key": str, "value": str}, {"confirm": str}),
        "mail": ({"target": str, "text": str, "client_key": str}, nothing),
        "mail_list": (nothing, {"member": str, "item": str, "after": str, "limit": int}),
    }
    if command not in shapes:
        raise Refusal("unknown command")
    _fields(request, *shapes[command])
    if len(request.get("reason") or "") > MAX_REASON:
        raise Refusal("reason is too long")
    return request


def failure(error: BaseException, *known: type[BaseException]) -> str:
    """Why a step failed, without a lower layer's message: our own refusals say what they
    say; a file error names the file; anything else is its type and the kernel's error code."""
    if isinstance(error, (Refusal, *known)):
        return clean(str(error), multiline=False)
    if isinstance(error, OSError):
        reason = error.strerror or type(error).__name__
        where = f": {error.filename}" if error.filename else ""
        return clean(f"{type(error).__name__}: {reason}{where}", multiline=False)
    code = getattr(error, "code", None)
    return type(error).__name__ + (f" ({code})" if isinstance(code, str) else "")


def kernel_uid() -> int:
    return pwd.getpwnam(KERNEL_USER).pw_uid


def check_host_file(path: Path) -> None:
    """The host file must be a plain file only the kernel's account or root can change."""
    try:
        info = path.lstat()
    except OSError as error:
        raise Refusal(f"cannot read the host file {path}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Refusal(f"the host file {path} is not a regular file")
    if info.st_uid not in (0, kernel_uid()) or info.st_mode & 0o002:
        raise Refusal(
            f"the host file {path} must belong to root or {KERNEL_USER}, not world-writable"
        )
    if info.st_mode & 0o020 and info.st_gid != pwd.getpwnam(KERNEL_USER).pw_gid:
        raise Refusal(f"the host file {path} is writable by a group other than {KERNEL_USER}")


class Result:
    """What a command printed and how it ended; this is the helper's whole reply."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.err: list[str] = []
        self.exit = 0
        self.files: dict[str, str] = {}
        self.status: str | None = None
        self.confirm: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "exit": self.exit,
            "out": [clean(line) for line in self.out],
            "err": [clean(line) for line in self.err],
            "files": self.files,
            "status": self.status,
            "confirm": self.confirm,
        }


def _number(value: str, key: str, whole: bool) -> int | float:
    pattern = r"[0-9]{1,9}" if whole else r"[0-9]{1,9}(\.[0-9]{1,6})?"
    if not re.fullmatch(pattern, value.strip()):
        raise Refusal(f"{key} must be {'a whole number' if whole else 'a number of dollars'}")
    return int(value) if whole else float(value)


def _email_limit(value: str) -> dict[str, int]:
    found = re.fullmatch(r"\s*([0-9]{1,6})\s*/\s*(?:([0-9]{1,4})\s*d?|day)\s*", value)
    if not found:
        raise Refusal("email.rate_limit must be COUNT/DAYS, for example 10/1 (ten every day)")
    return {"count": int(found[1]), "window_days": int(found[2] or 1)}


def settings_document(
    key: str, value: str, model: ModuleType, goals: dict[str, str]
) -> dict[str, Any]:
    """The settings document for `set <key> <value>`; `model.validate` then checks it, as the
    dashboard's settings page does."""
    group, _, name = key.partition(".")
    if key == "runs.team":
        return {"run_limits": {"team": _number(value, key, True)}}
    if group == "runs" and name:
        return {"run_limits": {"roles": {name: _number(value, key, True)}}}
    if key == "usd.team":
        return {"spend_limits": {"team_usd": _number(value, key, False)}}
    if key == "usd.per_run":
        return {"spend_limits": {"per_run_usd": _number(value, key, False)}}
    if group == "usd" and name:
        return {"spend_limits": {"roles_usd": {name: _number(value, key, False)}}}
    if group == "schedule" and name:
        crons = [] if value.strip().lower() == "none" else [c.strip() for c in value.split(";")]
        return {"schedules": {name: crons}}
    if key == "email.rate_limit":
        return {"email_rate_limit": _email_limit(value)}
    if key == "developers":
        return {"developers": _number(value, key, True)}
    if key == "model":
        return {"model": value.strip()}
    if key in ("goals", "non_goals"):
        other = "non_goals" if key == "goals" else "goals"
        kept = {other: goals[other]} if other in goals else {}
        if key == "non_goals" and "goals" not in kept:
            raise Refusal("the product has no goals yet: set goals first")
        return {"product_goals": {**kept, key: value}}
    known = ", ".join(k for k, _, _ in SET_KEYS)
    raise Refusal(f"{clean(key, multiline=False)[:64]!r} is not a setting key (keys: {known})")


class Team:
    """The commands, for one host file, as the kernel's account. Tests drive this directly;
    `connect` opens the supervisor's kernel client (default: the kit's)."""

    def __init__(
        self,
        host_path: Path,
        *,
        by: str | None = None,
        connect: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.path, self.by, self._connect = host_path, by, connect
        self.kit, self.model, self.applier, self.dashboard = modules()

    def connect(self, host: dict[str, Any]) -> Any:
        return (self._connect or self.kit.client)(host)

    def host(self) -> dict[str, Any]:
        try:
            found: dict[str, Any] = self.kit.load_host(self.path)
        except OSError as error:
            raise Refusal(f"cannot read the host file {self.path}: {error.strerror}") from None
        except ValueError as error:
            if isinstance(error, self.kit.HostError):
                raise Refusal(str(error)) from None
            raise Refusal(f"the host file {self.path} is not valid JSON") from None
        return found

    def run(self, request: dict[str, Any]) -> Result:
        """Carry out one checked request. Never raises: a failure is exit 1 and a message."""
        result = Result()
        known = (self.kit.HostError, self.model.SettingsError, self.applier.ApplyError)
        try:
            command = check_request(request)["command"]
            getattr(self, f"do_{command}")(request, result)
        except Exception as error:
            result.exit = 1
            result.err.append(f"refused: {failure(error, *known)}")
        return result

    # --- the kit's own commands ---------------------------------------------------------

    def do_status(self, request: dict[str, Any], result: Result) -> None:
        result.out += self.kit.status(self.host())

    def do_pause(self, request: dict[str, Any], result: Result) -> None:
        runs = self.kit.pause(self.host(), request.get("reason"))
        result.out.append(
            f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)"
        )
        result.out += [f"  {run}" for run in runs]

    def do_resume(self, request: dict[str, Any], result: Result) -> None:
        self.kit.resume(self.host())
        result.out.append("the supervisor is enabled; triggers admit again")

    def do_stop(self, request: dict[str, Any], result: Result) -> None:
        runs, problem = self.kit.stop(self.host())
        if problem:
            result.err.append(
                "the group was not deleted; delete it after `cardea team resume` "
                f"({clean(problem.split(':')[0], multiline=False)})"
            )
            result.exit = 1
        else:
            result.out.append("group deleted")
        result.out.append(
            f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)"
        )

    def do_apply(self, request: dict[str, Any], result: Result) -> None:
        result.out += self.kit.apply(self.host(), self.connect)

    def do_provision(self, request: dict[str, Any], result: Result) -> None:
        paused = bool(request.get("paused"))
        out = self.kit.provision(self.host(), paused=paused)
        result.out.append(f"provisioned {out}{' (supervisor disabled)' if paused else ''}")

    def do_render(self, request: dict[str, Any], result: Result) -> None:
        host = self.host()
        for warning in self.kit.wall_problems(host):
            result.err.append(f"WARNING: {warning}")
        with tempfile.TemporaryDirectory() as scratch:
            for path in self.kit.render(host, Path(scratch)):
                if FILE_NAME.fullmatch(path.name):
                    result.files[path.name] = path.read_text()

    # --- settings -----------------------------------------------------------------------

    def do_settings(self, request: dict[str, Any], result: Result) -> None:
        result.out += describe(self.host(), self.kit, self.model)

    def do_set(self, request: dict[str, Any], result: Result) -> None:
        """Preview the change; with the preview's token, apply exactly that change."""
        host = self.host()
        paused = bool(self.kit.read_pause_record(host))
        goals = self.model.current(host, self.kit, paused=paused)["product_goals"]
        wanted = settings_document(request["key"], request["value"], self.model, goals)
        found = self.applier.apply_settings(
            self.path, wanted, confirm=request.get("confirm"), by=self.by, connect=self.connect
        )
        result.status, result.confirm = found["status"], found["confirm"]
        result.out += self.model.render(found["preview"])

    # --- mail ---------------------------------------------------------------------------

    def backend(self, host: dict[str, Any], kernel: Any) -> Any:
        namespace, group, _, _ = self.kit._names(host)
        return self.dashboard.KernelBackend(kernel.request, namespace, group)

    def address(self, host: dict[str, Any], view: dict[str, Any], target: str) -> dict[str, str]:
        _, group, _, _ = self.kit._names(host)
        if target in ROLES:
            if target not in self.dashboard.roles(view):
                raise Refusal(f"the team has no role {target}")
            return {"pool": f"{group}.{target}"}
        if DEVELOPER.fullmatch(target):
            member = f"{group}.{target}"
            if member not in self.dashboard.members(view):
                raise Refusal(f"the team has no developer {target}")
            return {"instance": member}
        raise Refusal("the target is tl, pm, pe, ux or dev.N (developer N, from dev.0)")

    def do_mail(self, request: dict[str, Any], result: Result) -> None:
        text, key = request["text"], request["client_key"]
        if not text.strip() or len(text) > self.dashboard.MAX_MESSAGE_CHARS:
            raise Refusal(f"the message must be 1 to {self.dashboard.MAX_MESSAGE_CHARS} characters")
        if _TEXT_UNSAFE.search(text):
            raise Refusal("the message may not contain control characters")
        if not self.dashboard.IDENTIFIER.match(key):
            raise Refusal("the client key is not valid")
        host = self.host()
        kernel = self.connect(host)
        try:
            backend = self.backend(host, kernel)
            to = self.address(host, backend.overview(), request["target"])
            sent = backend.send_to(to, text, key)
        finally:
            kernel.close()
        result.out.append(f"sent to {request['target']} (message {sent.get('message_id')})")

    def do_mail_list(self, request: dict[str, Any], result: Result) -> None:
        base = self.dashboard
        query: dict[str, Any] = {
            k: request[k] for k in ("member", "item", "after") if request.get(k)
        }
        for name, pattern in (
            ("member", base.IDENTIFIER),
            ("item", base.ITEM),
            ("after", base.CURSOR),
        ):
            if name in query and not pattern.match(query[name]):
                raise Refusal(f"{name} is not valid")
        limit = request.get("limit", base.MAIL_LIMIT)
        if not 1 <= limit <= 50:
            raise Refusal("limit is 1 to 50")
        query["limit"] = limit
        host = self.host()
        kernel = self.connect(host)
        try:
            page = self.backend(host, kernel).mail(query)
        finally:
            kernel.close()
        result.out += mail_lines(page)


def describe(host: dict[str, Any], kit: ModuleType, model: ModuleType) -> list[str]:
    """The host's settings in plain words. Credentials, the kernel's socket and the supervisor's
    identity are not settings and are not shown."""
    record = kit.read_pause_record(host)
    paused = bool(record)
    now = model.current(host, kit, paused=paused)
    _, group, _, _ = kit._names(host)

    def money(value: object) -> str:
        return f"{value} USD" if value is not None else "no limit of its own"

    def count(value: object) -> str:
        return f"{value} runs" if value is not None else "no limit of its own"

    runs, spend = now["run_limits"], now["spend_limits"]
    lines = [
        f"Team {group} in namespace {host['namespace']}, working on repository "
        f"{host['repository']} (branch {host.get('default_branch', kit.DEFAULT_BRANCH)})."
    ]
    if paused:
        lines.append(f"The team is paused: {record.get('reason') or 'no reason recorded'}.")
    else:
        lines.append("The team is not paused.")
    lines.append(f"Developers: {now['developers']}. Model: {now['model']}.")
    lines.append(f"Runs a day: the whole team {count(runs['team'])}.")
    lines += [f"  {role}: {count(runs['roles'][role])}" for role in model.POOLS]
    lines.append(
        f"Spend a day: the whole team {money(spend['team_usd'])}; "
        f"one run at most {money(spend['per_run_usd'])}."
    )
    lines += [f"  {role}: {money(spend['roles_usd'][role])}" for role in model.POOLS]
    lines.append(f"Schedules (cron, {host.get('timezone', 'UTC')}):")
    for role in model.POOLS:
        lines.append(f"  {role}: {'; '.join(now['schedules'][role]) or 'none'}")
    limit = now["email_rate_limit"]
    if limit:
        lines.append(
            f"Email to the client ({host['email']['alias']}): at most {limit['count']} every "
            f"{limit['window_days']} day(s)."
        )
    else:
        lines.append("Email to the client: not set up.")
    goals = now["product_goals"]
    sizes = ", ".join(f"{k.replace('_', '-')} {len(v.encode())} bytes" for k, v in goals.items())
    lines.append(f"Product goals: {sizes or 'none'} (change with `cardea team set goals`).")
    return lines


def mail_lines(page: dict[str, Any]) -> list[str]:
    """A page of the team's mail as plain lines, newest first. The text is the team's, so
    it is data: only its printable characters are shown."""
    items = page.get("items") or []
    lines = [f"{len(items)} of {page.get('total', len(items))} message(s), newest first"]
    for message in items:
        sender = (message.get("from") or {}).get("address")
        recipient = (message.get("to") or {}).get("address")
        about = message.get("item") or {}
        thing = f" about {about.get('collection')}/{about.get('record_id')}" if about else ""
        when, message_id = message.get("sent_at"), message.get("message_id")
        lines.append(f"{when}  {sender} -> {recipient}{thing}  [{message_id}]")
        text = "\n".join(
            str(part["text"]) if isinstance(part, dict) and "text" in part else "[non-text part]"
            for part in message.get("parts") or []
        )
        if len(text) > MAX_SHOWN_TEXT:
            text = text[:MAX_SHOWN_TEXT] + " ... (cut)"
        lines += [f"    {line}" for line in text.splitlines() or [""]]
    if page.get("next"):
        lines.append(f"more: cardea team mail list --after {page['next']}")
    return lines


def execute(request: object, by: str | None) -> Result:
    """One request, checked and carried out against the fixed host file."""
    checked = check_request(request)
    check_host_file(DEFAULT_HOST)
    return Team(DEFAULT_HOST, by=by).run(checked)


def run_helper(argv: list[str]) -> int:
    """The program behind `/usr/libexec/cardea-team`: one JSON request on stdin, one JSON reply
    on stdout. It must be the kernel's account (the sudoers rule makes it so)."""
    reply = Result()
    try:
        if os.geteuid() != kernel_uid():
            raise Refusal(f"this helper runs as {KERNEL_USER}")
        os.chdir("/")
        os.umask(0o077)
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise Refusal("the request is too large")
        try:
            request = json.loads(raw)
        except ValueError:
            raise Refusal("the request is not JSON") from None
        reply = execute(request, os.environ.get("SUDO_USER"))
    except Exception as error:
        reply.exit = 1
        reply.err.append(f"refused: {failure(error)}")
    sys.stdout.write(json.dumps(reply.document()))
    return 0


# ---------------------------------------------------------------------------------------------
# The front end: the person's command line.
# ---------------------------------------------------------------------------------------------


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise Usage(message)

    def exit(self, status: int = 0, message: str | None = None) -> Any:
        raise Usage(message or "")


def parser() -> Parser:
    top = Parser(prog="cardea team", add_help=False)
    top.add_argument("-h", "--help", action="store_true")
    commands = top.add_subparsers(dest="command", parser_class=Parser)
    for name in ("status", "resume", "apply", "settings"):
        commands.add_parser(name, add_help=False)
    commands.add_parser("pause", add_help=False).add_argument("reason", nargs="*")
    commands.add_parser("stop", add_help=False).add_argument("--yes", action="store_true")
    commands.add_parser("render", add_help=False).add_argument("--out", type=Path)
    commands.add_parser("provision", add_help=False).add_argument("--paused", action="store_true")
    change = commands.add_parser("set", add_help=False)
    change.add_argument("--yes", action="store_true")
    change.add_argument("--dry-run", action="store_true")
    change.add_argument("key")
    change.add_argument("value", nargs="+")
    mail = commands.add_parser("mail", add_help=False)
    mail.add_argument("--member")
    mail.add_argument("--item")
    mail.add_argument("--after")
    mail.add_argument("--limit", type=int)
    mail.add_argument("words", nargs="+")
    return top


def ask(prompt: str) -> bool:
    """A yes from the person at the terminal. Never from a pipe: `--yes` is the explicit way."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        return False


def read_text(value: str) -> str:
    """A `set goals` value: text, `@FILE` or `-` (stdin), read as the person."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).expanduser().read_text()
    return value


def call_helper(request: dict[str, Any], *, direct: bool = False) -> dict[str, Any]:
    """Send one request as the kernel's account and return its reply."""
    if direct:
        document: dict[str, Any] = execute(request, None).document()
        return document
    done = subprocess.run(
        [SUDO, "-n", "-u", KERNEL_USER, HELPER],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    try:
        reply = json.loads(done.stdout)
        assert isinstance(reply, dict) and isinstance(reply.get("exit"), int)
    except (ValueError, AssertionError):
        detail = clean(
            done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "no reply",
            multiline=False,
        )
        raise Refusal(
            f"could not run {HELPER} as {KERNEL_USER} ({detail}); "
            f"you need to be in the group {OPERATORS_GROUP}"
        ) from None
    reply_out: dict[str, Any] = reply
    return reply_out


def show(reply: dict[str, Any]) -> int:
    for line in reply.get("out") or []:
        print(clean(str(line)))
    for line in reply.get("err") or []:
        print(clean(str(line)), file=sys.stderr)
    return int(reply["exit"])


def write_files(files: dict[str, str], out: Path) -> list[Path]:
    """`render`'s documents into `out`, as the person: private files, never through a link."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = []
    for name, text in sorted(files.items()):
        if not FILE_NAME.fullmatch(name):
            raise Refusal("the helper returned an unsafe file name")
        path = out / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        written.append(path)
    return written


def main(
    argv: list[str],
    *,
    send: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    confirm: Callable[[str], bool] = ask,
) -> int:
    """`cardea team ...`. `send` carries a request to the helper (tests replace it)."""
    if any(word in ("-h", "--help") for word in argv[: argv.index("--") if "--" in argv else None]):
        print(help_text())
        return 0
    try:
        args = parser().parse_args(argv)
    except Usage as error:
        if error.args and error.args[0]:
            print(f"cardea team: {error}\n", file=sys.stderr)
        print(help_text(), file=sys.stderr)
        return 2
    if args.help or not args.command:
        print(help_text())
        return 0
    transport = send
    if transport is None:
        try:
            direct = os.geteuid() == kernel_uid()
        except KeyError:
            print(
                f"cardea team: the account {KERNEL_USER} does not exist; "
                "is cardea-kernel installed?",
                file=sys.stderr,
            )
            return 1
        transport = lambda request: call_helper(request, direct=direct)  # noqa: E731
    try:
        return dispatch(args, transport, confirm)
    except (Refusal, OSError, subprocess.SubprocessError) as error:
        print(f"cardea team: {failure(error)}", file=sys.stderr)
        return 1


def dispatch(
    args: argparse.Namespace,
    send: Callable[[dict[str, Any]], dict[str, Any]],
    confirm: Callable[[str], bool],
) -> int:
    command = args.command
    if command == "mail":
        words = args.words
        if words == ["list"]:
            filters = {k: getattr(args, k) for k in ("member", "item", "after", "limit")}
            return show(
                send(
                    {"command": "mail_list", **{k: v for k, v in filters.items() if v is not None}}
                )
            )
        if len(words) < 2:
            raise Refusal(
                "mail needs a target and a text: cardea team mail <tl|pm|pe|ux|dev.N> <text>"
            )
        request = {
            "command": "mail",
            "target": words[0],
            "text": " ".join(words[1:]),
            "client_key": uuid.uuid4().hex,
        }
        return show(send(request))
    if command == "render":
        reply = send({"command": "render"})
        files = reply.get("files") or {}
        if reply["exit"] or not files:
            return show(reply)
        if args.out:
            return show({**reply, "out": [str(path) for path in write_files(files, args.out)]})
        code = show(reply)
        for name, text in sorted(files.items()):
            print(f"==> {name} <==\n{clean(text)}")
        return code
    if command == "pause":
        reason = " ".join(args.reason)
        return show(send({"command": "pause", **({"reason": reason} if reason else {})}))
    if command == "provision":
        return show(send({"command": "provision", "paused": args.paused}))
    if command == "stop":
        if not args.yes and not confirm(
            "Stop the team? This pauses it and deletes its agent group (records stay). [y/N] "
        ):
            print("not stopped; nothing changed")
            return 1
        return show(send({"command": "stop"}))
    if command == "set":
        return change(args, send, confirm)
    return show(send({"command": command}))


def change(
    args: argparse.Namespace,
    send: Callable[[dict[str, Any]], dict[str, Any]],
    confirm: Callable[[str], bool],
) -> int:
    """`set`: the helper previews, the person confirms, then the helper applies exactly the
    previewed change (its token is a digest of it; a moved host file makes it stale)."""
    value = read_text(" ".join(args.value))
    request = {"command": "set", "key": args.key, "value": value}
    preview = send(request)
    code = show(preview)
    if code or preview.get("status") == "unchanged":
        if not code:
            print("already in effect; nothing changed")
        return code
    if args.dry_run:
        print("dry run; nothing changed")
        return 0
    if not args.yes and not confirm("Apply this change? [y/N] "):
        print("not applied; nothing changed")
        return 1
    applied = send({**request, "confirm": preview["confirm"]})
    code = show(applied)
    if code:
        return code
    if applied.get("status") != "applied":
        print(
            "the team's settings changed since the preview; nothing applied. Run it again.",
            file=sys.stderr,
        )
        return 1
    print("applied")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--helper"]:
        raise SystemExit(run_helper(sys.argv[2:]))
    raise SystemExit(main(sys.argv[1:]))
