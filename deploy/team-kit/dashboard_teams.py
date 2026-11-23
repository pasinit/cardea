"""A read-only, multi-team dashboard on the owner's loopback interface (ADR-034 follows).

    python3 -I deploy/team-kit/dashboard_teams.py --teams TEAMS.json [--port 8766]
    python3 -I deploy/team-kit/dashboard_teams.py --fixtures dev=A.json aerarium=B.json

One page (`dashboard_teams.html`, `dashboard_teams.js`) shows each team's group overview side by
side. Each team is read through its own host file, the one `teamkit.py` uses: that file names the
kernel socket and the supervisor's credential source, so the credential is read by this process
and never reaches the page. The page and its API carry the overviews and, for a team that cannot
be read, a short error code. There is no message box and no write route.

Each team has a tab showing its state (running, stalled, paused or idle) and a mail view:
`GET /api/mail?team=NAME[&member=ID][&item=COLLECTION/RECORD][&after=CURSOR][&limit=N]` returns
one page of the team's messages with their full text, newest first. The kernel serves it to the
team's supervisor only (`GET /v0.2/groups/{name}/status/mail/text`), checking that authority live
on every read; a refusal is relayed as its error code. The page renders all text with
`textContent`, never as HTML or as a link.

Settings are edited on a second page, `/settings` (`dashboard_settings.html`,
`dashboard_settings.js`), for a team read through a host file:
`GET /api/settings?team=NAME` returns the team's settings as they are now;
`POST /api/settings/preview` `{team, settings}` returns what the change would alter and a
confirmation token, and changes nothing; `POST /api/settings/apply` `{team, settings, confirm}`
applies exactly what that preview showed (`teamsettings_apply.py`: render, provision, apply,
audit). Cancel is the page dropping the preview; a wrong or stale token is refused (409).
Only the team's supervisor may use any of the three: each first reads the group overview as the
supervisor, so the kernel's live check (`groups.manage`) refuses anyone else, and the apply's
own writes are checked again. This process decides nothing about authority. Secrets are not
settings: the host file's credentials and socket are never read into a response, and a
failure is reported by error code, never by a lower layer's message.

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
CONFIRM = re.compile(r"^[0-9a-f]{64}$")
STATIC = {
    "/": ("dashboard_teams.html", "text/html; charset=utf-8"),
    "/settings": ("dashboard_settings.html", "text/html; charset=utf-8"),
    "/dashboard_settings.js": ("dashboard_settings.js", "text/javascript; charset=utf-8"),
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


plain_reason = base.plain_reason
PLAIN_REASONS = base.PLAIN_REASONS
MAX_REASON = base.MAX_REASON


def error_code(error: Exception) -> str:
    """The kernel's error code only: never the exception text, which could carry anything."""
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and NAME.match(code) else "unreachable"


class TeamsBackend:
    """Reads every team's overview; `sources` maps a team name to a function returning its view.

    `mails` maps a team name to a function returning one page of its mail for a filter query.
    """

    def __init__(
        self,
        sources: dict[str, Callable[[], dict[str, Any]]],
        mails: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        hosts: dict[str, Path] | None = None,
        connect: Callable[[dict[str, Any]], Any] | None = None,
        pauses: dict[str, Callable[[], dict[str, Any]]] | None = None,
    ) -> None:
        self.sources, self.mails = sources, mails or {}
        self.pauses = pauses or {}
        self.hosts, self.connect = hosts or {}, connect

    def pause(self, name: str, code: str | None = None) -> dict[str, Any]:
        """Since when and why a team is paused, as text: the host's pause record, best effort.
        The record's path and the host file's credentials are never part of it."""
        try:
            record = self.pauses[name]() if name in self.pauses else {}
        except Exception:
            record = {}
        since = record.get("paused_at")
        reason = plain_reason(record.get("reason")) or plain_reason(code)
        return {"since": since if isinstance(since, str) else None, "reason": reason}

    def one(self, name: str) -> dict[str, Any]:
        try:
            view = self.sources[name]()
            state = base.team_state(view)
            found: dict[str, Any] = {"name": name, "state": state, "view": view}
            if state == "paused":
                found["pause"] = self.pause(name)
            return found
        except Exception as error:  # an unreachable team must not hide the others
            code = error_code(error)
            if code == "principal_disabled":  # a kernel that still refuses a paused team
                return {"name": name, "state": "paused", "pause": self.pause(name, code)}
            return {"name": name, "error": code}

    def overview(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as pool:
            return {"teams": list(pool.map(self.one, self.sources))}

    def status(self) -> dict[str, Any]:
        return self.overview()

    def replies(self) -> list[dict[str, Any]]:
        return []

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]:
        raise DashboardError("read_only", 405)

    def mail(self, query: dict[str, Any]) -> dict[str, Any]:
        team = query.get("team")
        if team not in self.sources:
            raise DashboardError("unknown_team", 404)
        read = self.mails.get(team)
        return read(query) if read else {"total": 0, "items": [], "next": None}

    def _settings_team(self, name: object) -> Path:
        """The team's host file, once the kernel has confirmed the caller is its supervisor."""
        if not isinstance(name, str) or name not in self.sources:
            raise DashboardError("unknown_team", 404)
        if name not in self.hosts:
            raise DashboardError("settings_unavailable", 404)
        self.sources[name]()  # the group overview: the kernel refuses a non-supervisor, live
        return self.hosts[name]

    def settings_current(self, query: dict[str, Any]) -> dict[str, Any]:
        path = self._settings_team(query.get("team"))
        kit, model, _ = apply_modules()
        host = kit.load_host(path)
        return {"settings": model.current(host, kit, paused=bool(kit.read_pause_record(host)))}

    def settings_change(self, step: str, body: object) -> dict[str, Any]:
        """Preview (`step` "preview") or confirm (`step` "apply") one settings change."""
        wanted = {"team", "settings"} | ({"confirm"} if step == "apply" else set())
        if not isinstance(body, dict) or set(body) != wanted:
            raise DashboardError("invalid_change")
        path = self._settings_team(body["team"])
        confirm = body.get("confirm")
        if step == "apply" and not (isinstance(confirm, str) and CONFIRM.match(confirm)):
            raise DashboardError("invalid_confirm")
        _, model, applier = apply_modules()
        try:
            found = applier.apply_settings(
                path, body["settings"], confirm=confirm, connect=self.connect
            )
        except model.SettingsError as error:
            raise DashboardError(f"invalid_settings: {error}", 422) from None
        except applier.ApplyError as error:
            raise DashboardError(f"refused: {error}", 422) from None
        if step == "apply" and found["status"] == "needs_confirmation":
            raise DashboardError("confirmation_mismatch", 409)
        return {
            "status": found["status"],
            "confirm": found["confirm"],
            "changes": found["preview"]["changes"],
            "problems": found["preview"]["problems"],
            "lines": model.render(found["preview"]),
        }


def apply_modules() -> tuple[ModuleType, ModuleType, ModuleType]:
    """The team kit, the settings model and the apply module."""
    applier = load("teamsettings_apply")
    kit, model = applier._modules()
    return kit, model, applier


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


def pause_source(host_file: Path) -> Callable[[], dict[str, Any]]:
    kit = load("teamkit")
    host = kit.load_host(host_file)
    return lambda: dict(kit.read_pause_record(host))


def fixture_source(path: Path) -> Callable[[], dict[str, Any]]:
    view = json.loads(path.read_text())
    return lambda: view


def server(backend: TeamsBackend, port: int = DEFAULT_PORT, bind: str = base.LOOPBACK) -> Any:
    return base.server(backend, port, bind, static=STATIC, writable=False)  # settings: own routes


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
            kernels = {n: kernel_source(h) for n, h in read_teams(args.teams)}
            backend = TeamsBackend(
                {n: read for n, (read, _) in kernels.items()},
                {n: mail for n, (_, mail) in kernels.items()},
                dict(read_teams(args.teams)),
                pauses={n: pause_source(h) for n, h in read_teams(args.teams)},
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
