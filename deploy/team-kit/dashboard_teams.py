"""A read-only, multi-team dashboard on the owner's loopback interface (ADR-034 follows).

    python3 -I deploy/team-kit/dashboard_teams.py --teams TEAMS.json [--port 8766]
    python3 -I deploy/team-kit/dashboard_teams.py --fixtures dev=A.json aerarium=B.json

One page (`dashboard_teams.html`, `dashboard_teams.js`) shows each team's group overview side by
side. Each team is read through its own host file, the one `teamkit.py` uses: that file names the
kernel socket and the supervisor's credential source, so the credential is read by this process
and never reaches the page. The page and its API carry the overviews and, for a team that cannot
be read, a short error code. There is no message box and no write route.

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
    """Reads every team's overview; `sources` maps a team name to a function returning its view."""

    def __init__(self, sources: dict[str, Callable[[], dict[str, Any]]]) -> None:
        self.sources = sources

    def one(self, name: str) -> dict[str, Any]:
        try:
            return {"name": name, "view": self.sources[name]()}
        except Exception as error:  # an unreachable team must not hide the others
            return {"name": name, "error": error_code(error)}

    def overview(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=max(1, len(self.sources))) as pool:
            return {"teams": list(pool.map(self.one, self.sources))}

    def send(self, role: str, text: str, client_key: str) -> dict[str, Any]:
        raise DashboardError("read_only", 405)


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


def kernel_source(host_file: Path) -> Callable[[], dict[str, Any]]:
    """One team's overview, read as its supervisor through its own host file."""
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

    return read


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
            sources = {n: kernel_source(h) for n, h in read_teams(args.teams)}
        else:
            pairs = [item.partition("=") for item in args.fixtures]
            sources = {n: fixture_source(Path(f)) for n, _, f in pairs}
        httpd = server(TeamsBackend(sources), args.port, args.bind)
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
