"""`cardea team`: run the agent dev team from the command line, as a plain user.

    cardea team status | pause [reason] | resume | stop | apply | render | provision
    cardea team settings
    cardea team set <key> <value>
    cardea team mail <tl|pm|pe|ux|dev.N> <text>
    cardea team mail list

The commands are the team kit's own (`deploy/team-kit`, installed under `/usr/lib/cardea/teamkit`):
this module parses, confirms and relays, and decides nothing about authority. Every command acts
as the team's supervisor through the host file, so it runs as `cardea-kernel`, the account that
owns that file, the kernel socket and the supervisor's identity. A plain user in the
`cardea-operators` group gets there through one narrow sudoers rule that allows the root-owned
helper `/usr/libexec/cardea-team`, with no arguments, as `cardea-kernel`. The request travels on
the helper's standard input, never on its command line, and the helper reads only the fixed host
file: `--host` is honoured only for a caller that already is `cardea-kernel`.

`set` shows what would change (the dashboard Settings' own validation and preview), asks, and
applies exactly what was shown: the confirmation is a digest of the previewed changes.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pwd
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO
from urllib.parse import urlencode

KERNEL_USER = "cardea-kernel"
OPERATORS_GROUP = "cardea-operators"
HOST_FILE = Path("/etc/cardea/team.host.json")
STATE_DIR = Path("/var/lib/cardea/team")
HELPER = "/usr/libexec/cardea-team"
# No argument after the helper: the sudoers rule allows it only without any.
SUDO: tuple[str, ...] = ("/usr/bin/sudo", "-n", "-u", KERNEL_USER, HELPER)
# Where the kit keeps its records when the host file does not say: the one directory the
# kernel account may write (`/etc/cardea` is root's), and the installed `cardea`.
PACKAGE_PROVISIONING: dict[str, Any] = {
    "out": str(STATE_DIR / "provision.json"),
    "cardea": ["/usr/bin/cardea"],
}
# Set from a test or a source checkout; otherwise the installed kit, then this checkout's.
KIT_DIR: Path | None = None

COMMANDS = (
    ("status", "the team overview: members, runs, work, mail, approvals, spend"),
    ("pause", "cancel every unfinished run and admit nothing until `resume` (optional reason)"),
    ("resume", "enable the supervisor again, so triggers admit"),
    ("stop", "pause, then delete the group (its members drain)"),
    ("apply", "put the collections and the group to the kernel as the supervisor"),
    ("render", "write the documents the team is made of, for review"),
    ("provision", "merge the team's principals and grants into the provisioning document"),
    ("settings", "show the team's settings as they are now"),
    ("set", "change one setting: preview the difference, confirm, apply"),
    ("mail", "mail a role or developer (`mail pm TEXT`), or list the team's mail (`mail list`)"),
)
MAIL_TARGET = re.compile(r"(tl|pm|pe|ux|dev\.[0-9]{1,3})")
# Settings whose value is plain text: everything else is read as JSON (a number, true/false,
# a list or an object), and a schedule as cron expressions separated by `;`.
TEXT_KEYS = ("model", "product_goals.goals", "product_goals.non_goals", "paused.reason")
WHOLE_SETTINGS = ("email_rate_limit", "product_goals")
MAX_REQUEST_BYTES = 262144
MAX_ARGUMENTS = 64
MAX_ARGUMENT_CHARS = 16384
MAX_SHOWN = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

Transport = Callable[[list[str]], tuple[int, str, str]]


class CliError(Exception):
    """A refusal to report as one line; the message holds no secret."""


def kit_dir() -> Path:
    here = Path(__file__).resolve()
    found = [KIT_DIR] if KIT_DIR else []
    parents = list(here.parents)
    found.append(parents[2] / "teamkit")
    if len(parents) > 5:
        found.append(parents[5] / "deploy" / "team-kit")
    for candidate in found:
        if (candidate / "teamkit.py").is_file():
            return candidate
    raise CliError("the team kit is not installed (no teamkit.py beside the kernel's code)")


def load(name: str) -> ModuleType:
    """A kit module, loaded by path like the kit loads its own siblings."""
    spec = importlib.util.spec_from_file_location(name, kit_dir() / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return sys.modules[name]


def modules() -> tuple[ModuleType, ModuleType, ModuleType]:
    """The team kit, the settings model and the apply module, sharing one kit instance."""
    applier = load("teamsettings_apply")
    kit, model = applier._modules()
    kit.PROVISIONING_DEFAULTS.update(PACKAGE_PROVISIONING)
    return kit, model, applier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cardea team",
        description="Run the agent dev team: status, pause, resume, stop, settings and mail. "
        "Commands act as the team's supervisor, through /usr/libexec/cardea-team for members "
        f"of the {OPERATORS_GROUP} group.",
    )
    parser.add_argument(
        "--host",
        type=Path,
        help=f"host file; only honoured when already running as {KERNEL_USER} "
        f"(otherwise {HOST_FILE})",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--host", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub = {
        name: commands.add_parser(name, help=text, description=text, parents=[shared])
        for name, text in COMMANDS
    }
    sub["pause"].add_argument("reason", nargs="*", help="why, kept for `status` (no secrets)")
    sub["render"].add_argument(
        "--out", type=Path, help=f"directory (default {STATE_DIR}/rendered)"
    )
    sub["provision"].add_argument("--paused", action="store_true", help="write it disabled")
    sub["settings"].add_argument("--json", action="store_true", help="as JSON")
    change = sub["set"]
    change.add_argument("key", help="a setting field, e.g. developers, run_limits.roles.dev")
    change.add_argument("value", nargs="+", help="the new value (JSON; text for text settings)")
    change.add_argument("-y", "--yes", action="store_true", help="apply without asking")
    change.add_argument("--confirm", metavar="TOKEN", help="apply the preview with this token")
    change.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    mail = sub["mail"]
    mail.add_argument(
        "words", nargs="+", metavar="TARGET TEXT", help="tl, pm, pe, ux or dev.N, then the text"
    )
    mail.add_argument("--member", help="`mail list`: only this member's mail")
    mail.add_argument("--item", help="`mail list`: only mail about this record (collection/id)")
    mail.add_argument("--after", help="`mail list`: the cursor a previous page printed")
    mail.add_argument("--limit", type=int, default=20, help="`mail list`: messages (1-50)")
    return parser


# ---- the commands, run as the kernel identity ----


def describe(error: Exception) -> str:
    """One line for a failure, never a lower layer's message (which could carry a secret): the
    kit's own refusals hold none, and anything else is named by type and kernel code."""
    name = type(error).__name__
    if isinstance(error, CliError) or name in (
        "HostError",
        "SettingsError",
        "ApplyError",
        "DashboardError",
    ):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return "a file the command needs is missing"
    if isinstance(error, PermissionError):
        return "a file the command needs is not readable or writable by this account"
    code = getattr(error, "code", None)
    return name + (f" ({code})" if isinstance(code, str) else "")


def invoker() -> str:
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return f"uid {os.getuid()}"


def say(*lines: str) -> None:
    print("\n".join(lines))


def one_line(text: object) -> str:
    shown = _CONTROL.sub(" ", str(text)).strip()
    return shown if len(shown) <= MAX_SHOWN else shown[: MAX_SHOWN - 3] + "..."


def flatten(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict) and value:
        out: list[tuple[str, Any]] = []
        for key, child in value.items():
            out += flatten(child, f"{path}.{key}" if path else str(key))
        return out
    return [(path, value)]


def current_lines(current: dict[str, Any]) -> list[str]:
    lines = []
    for field, value in flatten(current):
        if field.startswith("product_goals.") and isinstance(value, str):
            lines.append(f"{field}: {len(value.encode())} bytes")  # the text itself is not shown
        else:
            lines.append(f"{field}: {json.dumps(value)}")
    return lines


def parse_value(key: str, raw: str) -> Any:
    if key in TEXT_KEYS:
        return raw
    if key.startswith("schedules.") and not raw.lstrip().startswith("["):
        return [part.strip() for part in raw.split(";") if part.strip()]
    try:
        return json.loads(raw)
    except ValueError:
        raise CliError(
            "the value must be JSON: a number, true, false, a list or an object "
            "(schedules: cron expressions separated by `;`)"
        ) from None


def change_document(
    model: ModuleType, key: str, raw: str, current: dict[str, Any]
) -> dict[str, Any]:
    """The settings document for `set KEY VALUE`, validated as the dashboard Settings are.

    A setting that is replaced whole (the email rate limit, the product goals) keeps its other
    fields: setting one of them carries the rest over from `current`."""
    path = key.split(".")
    if not all(path) or path[0] not in model.SETTINGS:
        raise CliError(f"{key} is not a setting (known: {', '.join(model.SETTINGS)})")
    if key == "paused":
        path = ["paused", "paused"]
    document: dict[str, Any] = {}
    node = document
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = parse_value(key, raw)
    if len(path) == 2 and path[0] in WHOLE_SETTINGS:
        for name, value in (current.get(path[0]) or {}).items():
            if value is not None:
                node.setdefault(name, value)
    checked: dict[str, Any] = model.validate(document)
    return checked


def set_command(args: argparse.Namespace, host_path: Path) -> int:
    kit, model, applier = modules()
    host = kit.load_host(host_path)
    current = model.current(host, kit, paused=bool(kit.read_pause_record(host)))
    wanted = change_document(model, args.key, " ".join(args.value), current)
    found = applier.apply_settings(host_path, wanted, confirm=args.confirm, by=invoker())
    lines = model.render(found["preview"])
    if args.json:
        say(json.dumps({"status": found["status"], "confirm": found["confirm"], "lines": lines}))
        return 0
    say(*lines)
    return {"applied": 0, "unchanged": 0}.get(found["status"], 2)


def mail_command(args: argparse.Namespace, host_path: Path) -> int:
    kit, _, _ = modules()
    dashboard = load("dashboard")
    host = kit.load_host(host_path)
    namespace, group, _, _ = kit._names(host)
    words = args.words
    kernel = kit.client(host)
    try:
        if words[0] == "list":
            if len(words) > 1:
                raise CliError("`mail list` takes no text; filter with --member, --item, --after")
            filters = {"member": args.member, "item": args.item, "after": args.after}
            query = dashboard.mail_query(
                urlencode({**{k: v for k, v in filters.items() if v}, "limit": args.limit})
            )
            page = dashboard.KernelBackend(kernel.request, namespace, group).mail(query)
            return show_mail(page)
        target, text = words[0], " ".join(words[1:])
        if not MAIL_TARGET.fullmatch(target):
            raise CliError("mail goes to tl, pm, pe, ux or dev.N (e.g. dev.0), then the text")
        if not text.strip() or len(text) > dashboard.MAX_MESSAGE_CHARS:
            raise CliError(f"the text must be 1 to {dashboard.MAX_MESSAGE_CHARS} characters")
        if target.startswith("dev."):
            members = kit.developers(host)
            address = f"{group}.{target}"
            if address not in members:
                raise CliError(
                    f"this team has {len(members)} developers: dev.0 to dev.{len(members) - 1}"
                )
            to = {"instance": address}
        else:
            address = f"{group}.{target}"
            to = {"pool": address}
        sent = kernel.request(
            "POST",
            "/mailbox",
            json={
                "namespace": namespace,
                "client_key": f"cli-{uuid.uuid4().hex}",
                "to": to,
                "parts": [{"text": text}],
            },
        )
        say(f"sent to {address} (message {one_line(sent.get('message_id'))})")
        return 0
    finally:
        kernel.close()


def show_mail(page: dict[str, Any]) -> int:
    for message in page.get("items", []):
        text = " ".join(str(p["text"]) for p in message.get("parts", []) if "text" in p)
        about = message.get("item")
        record = (
            f" [{one_line(about.get('collection'))}/{one_line(about.get('record_id'))}]" if about else ""
        )
        say(
            f"{one_line(message.get('sent_at'))} {one_line(message['from']['address'])} -> "
            f"{one_line(message['to']['address'])}{record}: {one_line(text)}"
        )
    say(f"{len(page.get('items', []))} of {page.get('total', 0)} message(s)")
    if page.get("next"):
        say(f"more: cardea team mail list --after {page['next']}")
    return 0


def execute(args: argparse.Namespace, host_path: Path, *, helper: bool) -> int:
    """One command, as the kernel identity, on the host file at `host_path`."""
    kit, model, _ = modules()
    command = args.command
    if command == "set":
        return set_command(args, host_path)
    if command == "mail":
        return mail_command(args, host_path)
    host = kit.load_host(host_path)
    if command == "status":
        say(*kit.status(host))
    elif command == "settings":
        current = model.current(host, kit, paused=bool(kit.read_pause_record(host)))
        say(json.dumps(current, indent=2) if args.json else "\n".join(current_lines(current)))
    elif command == "pause":
        runs = kit.pause(host, " ".join(args.reason) or None)
        say(f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)")
        say(*(f"  {run}" for run in runs))
    elif command == "resume":
        kit.resume(host)
        say("the supervisor is enabled; triggers admit again")
    elif command == "stop":
        runs, problem = kit.stop(host)
        if problem:
            # The kit's problem is "Type: message"; the message is a lower layer's, so not shown.
            print(
                f"the group was not deleted ({problem.partition(':')[0]}); "
                "delete it after `resume`",
                file=sys.stderr,
            )
        else:
            say("group deleted")
        say(f"the supervisor is disabled; the kernel cancelled its {len(runs)} active run(s)")
        return 1 if problem else 0
    elif command == "apply":
        say(*kit.apply(host, kit.client))
    elif command == "render":
        out = args.out or STATE_DIR / "rendered"
        if helper and not out.resolve().is_relative_to(STATE_DIR.resolve()):
            raise CliError(f"through the helper, documents are written under {STATE_DIR} only")
        for warning in kit.wall_problems(host):
            print(f"WARNING: {warning}", file=sys.stderr)
        say(*(str(path) for path in kit.render(host, out)))
    elif command == "provision":
        if kit.read_pause_record(host) and not args.paused:
            raise CliError("the team is paused: `resume` it, or provision with --paused")
        say(str(kit.provision(host, paused=args.paused)))
    return 0


def run_argv(argv: Sequence[str], host_path: Path, *, helper: bool) -> int:
    """Parse and execute; every failure becomes one line on standard error and an exit code."""
    try:
        args = build_parser().parse_args(list(argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    if helper and args.host:
        print("cardea team: --host is not honoured through the helper", file=sys.stderr)
        return 2
    try:
        return execute(args, host_path, helper=helper)
    except Exception as error:
        print(f"cardea team: {describe(error)}", file=sys.stderr)
        return 1


def helper_main(stdin: TextIO) -> int:
    """The root-owned helper's entry, running as `cardea-kernel`: one request on standard input,
    `{"argv": [...]}`, executed on the fixed host file and nothing else."""
    raw = stdin.read(MAX_REQUEST_BYTES + 1)
    try:
        request = json.loads(raw) if len(raw) <= MAX_REQUEST_BYTES else None
    except ValueError:
        request = None
    argv = request.get("argv") if isinstance(request, dict) and set(request) == {"argv"} else None
    if not (
        isinstance(argv, list)
        and 0 < len(argv) <= MAX_ARGUMENTS
        and all(isinstance(a, str) and len(a) <= MAX_ARGUMENT_CHARS for a in argv)
    ):
        print("cardea team: the helper takes one request on standard input", file=sys.stderr)
        return 2
    return run_argv(argv, HOST_FILE, helper=True)


# ---- the caller: a plain user, or the kernel identity itself ----


def running_as_kernel() -> bool:
    try:
        return os.geteuid() == pwd.getpwnam(KERNEL_USER).pw_uid
    except KeyError:
        return False


def direct_transport(host_path: Path) -> Transport:
    def run(argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_argv(argv, host_path, helper=False)
        return code, out.getvalue(), err.getvalue()

    return run


def sudo_transport(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(SUDO), input=json.dumps({"argv": argv}), capture_output=True, text=True
        )
    except OSError:
        return 127, "", "cardea team: sudo cannot be run\n"
    err = done.stderr
    if done.returncode and "sudo:" in err:
        err += f"cardea team: team commands need membership of the {OPERATORS_GROUP} group\n"
    return done.returncode, done.stdout, err


def relay(result: tuple[int, str, str]) -> int:
    code, out, err = result
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


def interactive_set(
    args: argparse.Namespace, transport: Transport, ask: Callable[[str], str]
) -> int:
    """Preview, ask, apply exactly the preview: two requests, the token between them."""
    request = ["set", args.key, *args.value]
    stale = "cardea team: the settings changed since that preview; nothing applied"

    def step(extra: list[str]) -> tuple[int, dict[str, Any]]:
        code, out, err = transport(request + extra + ["--json"])
        if code:
            relay((code, "", err))
            return code, {}
        found: dict[str, Any] = json.loads(out)
        return 0, found

    code, found = step(["--confirm", args.confirm] if args.confirm else [])
    if code:
        return code
    say(*found["lines"])
    if found["status"] == "unchanged":
        say("already in effect; nothing changed")
        return 0
    if found["status"] == "applied":
        say("applied")
        return 0
    if args.confirm:
        print(stale, file=sys.stderr)
        return 1
    if not args.yes:
        try:
            answer = ask("Apply this change? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            say("nothing changed")
            return 1
    code, found = step(["--confirm", found["confirm"]])
    if code:
        return code
    if found["status"] != "applied":
        print(stale, file=sys.stderr)
        return 1
    say("applied")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    ask: Callable[[str], str] = input,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--helper"]:
        return helper_main(sys.stdin)
    parser = build_parser()
    args = parser.parse_args(arguments)
    if transport is None:
        if running_as_kernel():
            transport = direct_transport(args.host or HOST_FILE)
        elif args.host:
            parser.error(f"--host is only honoured when running as {KERNEL_USER}")
        else:
            transport = sudo_transport
    if args.command == "set":
        return interactive_set(args, transport, ask)
    return relay(transport(arguments))
