"""A read-only, multi-team dashboard on the owner's loopback interface (ADR-034 follows).

    python3 -I deploy/team-kit/dashboard_teams.py --teams TEAMS.json [--port 8766]
    python3 -I deploy/team-kit/dashboard_teams.py --fixtures dev=A.json aerarium=B.json

One page (`dashboard_teams.html`, `dashboard_teams.js`) shows each team's group overview side by
side. Each team is read through its own host file, the one `teamkit.py` uses: that file names the
kernel socket and the supervisor's credential source, so the credential is read by this process
and never reaches the page. The page and its API carry the overviews and, for a team that cannot
be read, a short error code. There is no message box. The one write is the Settings section.

Each team has a tab showing its state (running, stalled, paused or idle) and a mail view:
`GET /api/mail?team=NAME[&member=ID][&item=COLLECTION/RECORD][&after=CURSOR][&limit=N]` returns
one page of the team's messages with their full text, newest first. The kernel serves it to the
team's supervisor only (`GET /v0.2/groups/{name}/status/mail/text`), checking that authority live
on every read; a refusal is relayed as its error code. The page renders all text with
`textContent`, never as HTML or as a link.

Settings: `POST /api/settings/preview` and `POST /api/settings/apply`, each with
`{"team": NAME, "settings": {...}}` (apply also `"confirm"`, the token the preview returned), are
the team kit's `teamsettings_apply.apply_settings`, unchanged: a preview changes nothing, and an
apply goes through the kit's render, provision and apply, is audited, and puts the old settings
back if the kernel refuses. Before either, and for a pause or resume too, the kernel is asked
for a read only the group's supervisor may make (the mail text), as the supervisor; anyone else,
or a disabled supervisor, is refused with the kernel's code and nothing is previewed or changed.
Goals text goes to the server but is shown only by size; no setting is a secret and none is
echoed. Not offered with `--fixtures`. A paused team's supervisor is disabled, so it is resumed
with `teamkit.py resume`, not from the page.

TEAMS.json is `{"teams": [{"name": "dev", "host": "path/to/host.json"}, ...]}`. One team being
unreachable shows an error in its column; the others still render.
"""

import argparse
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8766
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AUTHORITY_NOTE = (
    "the kernel confirmed you are this team's supervisor, and checks again on apply"
)
SETTINGS_BODY = {"team", "settings", "confirm"}
STATIC = {
    "/": ("dashboard_teams.html", "text/html; charset=utf-8"),
    "/dashboard_teams.js": ("dashboard_teams.js", "text/javascript; charset=utf-8"),
}


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


base = load("dashboard")
DashboardError = base.DashboardError


def error_code(error: Exception) -> str:
    """The kernel's error code only: never the exception text, which could carry anything."""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and NAME.match(code) else "unreachable"


class TeamsBackend:
    """Reads every team's overview; `sources` maps a team name to a function returning its view.

    `mails` maps a team name to a function returning one page of its mail for a filter query.
    `editors` maps a team name to a function that previews or applies a settings change.
    """

    def __init__(
        self,
        sources: dict[str, Callable[[], dict[str, Any]]],
        mails: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        editors: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        self.sources, self.mails, self.editors = sources, mails or {}, editors or {}

    def one(self, name: str) -> dict[str, Any]:
        try:
            view = self.sources[name]()
            found = {"name": name, "state": base.team_state(view), "view": view}
            return {**found, "editable": True} if name in self.editors else found
        except Exception as error:  # an unreachable team must not hide the others
            return {"name": name, "error": error_code(error)}

    def overview(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as pool:
            return {"teams": list(pool.map(self.one, self.sources))}

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]:
        raise DashboardError("read_only", 405)

    def settings(self, action: str, body: Any) -> dict[str, Any]:
        """Preview or apply one team's settings; `body` is `{team, settings[, confirm]}`."""
        if not isinstance(body, dict) or not {"team", "settings"} <= set(body) <= SETTINGS_BODY:
            raise DashboardError("invalid_settings")
        team, confirm = body["team"], body.get("confirm")
        if not isinstance(team, str) or not (confirm is None or isinstance(confirm, str)):
            raise DashboardError("invalid_settings")
        if not self.editors:
            raise DashboardError("settings_unavailable", 404)
        if team not in self.editors:
            raise DashboardError("unknown_team", 404)
        if action == "apply" and not confirm:
            raise DashboardError("confirmation_required")
        return self.editors[team](action, {"settings": body["settings"], "confirm": confirm})

    def mail(self, query: dict[str, Any]) -> dict[str, Any]:
        team = query.get("team")
        if team not in self.sources:
            raise DashboardError("unknown_team", 404)
        read = self.mails.get(team)
        return read(query) if read else {"total": 0, "items": [], "next": None}


def read_teams(path: Path) -> list[tuple[str, Path]]:
    document = json.loads(path.read_text())
    entries = document.get("teams") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise DashboardError("teams file needs a non-empty `teams` list")
    teams: list[tuple[str, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "host"}:
            raise DashboardError("each team has exactly `name` and `host`")
        name = entry["name"]
        if not isinstance(name, str) or not NAME.match(name):
            raise DashboardError(f"invalid team name {name!r}")
        if name in [n for n, _ in teams]:
            raise DashboardError(f"duplicate team {name!r}")
        teams.append((name, (path.parent / str(entry["host"])).expanduser()))
    return teams


def kernel_source(
    host_file: Path,
) -> tuple[Callable[[], dict[str, Any]], Callable[[dict[str, Any]], dict[str, Any]]]:
    """One team's overview and its mail, read as its supervisor through its own host file."""
    kit = load("teamkit")
    host = kit.load_host(host_file)
    namespace, group, _, _ = kit._names(host)

    def read() -> dict[str, Any]:
        kernel = kit.client(host)
        try:
            view: dict[str, Any] = base.KernelBackend(kernel.request, namespace, group).overview()
            return view
        finally:
            kernel.close()

    def read_mail(query: dict[str, Any]) -> dict[str, Any]:
        kernel = kit.client(host)
        try:
            backend = base.KernelBackend(kernel.request, namespace, group)
            result: dict[str, Any] = backend.mail(query)
            return result
        finally:
            kernel.close()

    return read, read_mail


def settings_editor(
    host_file: Path, connect: Callable[[dict[str, Any]], Any] | None = None
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """One team's settings preview and apply, through the team kit's `apply_settings`.

    Each call first makes the supervisor-only read as the supervisor, so the kernel (not this
    process) decides who may change settings, live, for a pause or resume as for any other
    setting. The host file is read afresh each time: it is what an apply changes."""
    kit = load("teamkit")
    applier = load("teamsettings_apply")
    invalid = applier._modules()[1].SettingsError
    connect = connect or kit.client

    def edit(action: str, body: dict[str, Any]) -> dict[str, Any]:
        host = kit.load_host(host_file)
        namespace, group, supervisor, _ = kit._names(host)
        kernel = connect(host)
        try:
            kernel.request(
                "GET",
                f"/groups/{group}/status/mail/text",
                params={"namespace": namespace, "limit": 1},
            )
        finally:
            kernel.close()
        try:
            found = applier.apply_settings(
                host_file,
                body["settings"],
                confirm=body["confirm"] if action == "apply" else None,
                by=f"{supervisor} (dashboard)",
                connect=connect,
            )
        except invalid as error:  # names the field, never the value
            raise DashboardError("invalid_settings", 400, str(error)) from None
        except applier.ApplyError as error:
            raise DashboardError("not_applied", 409, str(error)) from None
        if action == "apply" and found["status"] == "needs_confirmation":
            raise DashboardError("confirmation_stale", 409)
        return shown(found)

    return edit


def size(text: object) -> dict[str, int] | None:
    return None if text is None else {"bytes": len(str(text).encode())}


def shown(found: dict[str, Any]) -> dict[str, Any]:
    """An `apply_settings` result for the page: goals text by size only. The kit's note that a
    preview checks no authority does not hold here: the editor asked the kernel first."""
    changes = []
    for change in found["preview"]["changes"]:
        hidden = change["setting"] == "product_goals"
        changes.append(
            {
                "setting": change["setting"],
                "field": change["field"],
                "from": size(change["from"]) if hidden else change["from"],
                "to": size(change["to"]) if hidden else change["to"],
            }
        )
    return {
        "status": found["status"],
        "confirm": found["confirm"],
        "changes": changes,
        "problems": found["preview"]["problems"],
        "authority": AUTHORITY_NOTE,
        "audit_id": found["audit_id"],
    }


def fixture_source(path: Path) -> Callable[[], dict[str, Any]]:
    view = json.loads(path.read_text())
    return lambda: view


def server(backend: TeamsBackend, port: int = DEFAULT_PORT, bind: str = base.LOOPBACK) -> Any:
    return base.server(backend, port, bind, static=STATIC, writable=False)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--teams", type=Path, help="the teams file, naming one host file each")
    source.add_argument("--fixtures", nargs="+", metavar="NAME=FILE", help="recorded overviews")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=base.LOOPBACK, help="loopback only; others are refused")
    args = parser.parse_args(argv)
    try:
        if args.teams:
            hosts = read_teams(args.teams)
            kernels = {n: kernel_source(h) for n, h in hosts}
            backend = TeamsBackend(
                {n: read for n, (read, _) in kernels.items()},
                {n: mail for n, (_, mail) in kernels.items()},
                {n: settings_editor(h) for n, h in hosts},
            )
        else:
            pairs = [item.partition("=") for item in args.fixtures]
            backend = TeamsBackend({n: fixture_source(Path(f)) for n, _, f in pairs})
        httpd = server(backend, args.port, args.bind)
    except DashboardError as error:
        print(f"dashboard: {error.code}", file=sys.stderr)
        return 2
    print(f"dashboard on http://{base.LOOPBACK}:{httpd.server_port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
