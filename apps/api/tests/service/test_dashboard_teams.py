"""The multi-team dashboard (deploy/team-kit/dashboard_teams.py): read-only, loopback-only,
one team failing does not hide the others, and no credential reaches the page."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from test_dashboard import KIT, fixture
from test_dashboard import call as raw_call

from cardea.service.client import ClientError

TOKEN = "s3cr3t-token-value"


def call(*args: Any, **kwargs: Any) -> tuple[int, Any]:
    return raw_call(*args, build=teams().server, **kwargs)


def teams() -> Any:
    spec = importlib.util.spec_from_file_location("dashboard_teams", KIT / "dashboard_teams.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def aerarium() -> dict[str, Any]:
    return json.loads((KIT / "dashboard.fixture.aerarium.json").read_text())  # type: ignore[no-any-return]


def backend(**extra: Any) -> Any:
    module = teams()
    sources = {"dev": lambda: fixture(), "aerarium": lambda: aerarium(), **extra}
    return module.TeamsBackend(sources)


def test_two_teams_render_side_by_side() -> None:
    result = call(backend(), "GET", "/api/status")
    assert result[0] == 200
    names = [t["name"] for t in result[1]["teams"]]
    assert names == ["dev", "aerarium"]
    assert result[1]["teams"][0]["view"] == fixture()
    assert result[1]["teams"][1]["view"]["group"] == "aerarium-team"
    status, page = call(
        backend(),
        "GET",
        "/",
    )
    assert status == 200 and '<script src="/dashboard_teams.js">' in page
    script = (KIT / "dashboard_teams.js").read_text()
    assert (
        "innerHTML" not in script and "display: flex" in (KIT / "dashboard_teams.html").read_text()
    )


def raising(error: Exception) -> Any:
    def read() -> dict[str, Any]:
        raise error

    return read


def test_an_unreachable_team_shows_an_error_and_hides_nothing() -> None:
    down = backend(aerarium=raising(OSError(f"connect failed, Bearer {TOKEN}")))
    status, body = call(down, "GET", "/api/status")
    assert status == 200
    dev, other = body["teams"]
    assert dev["view"] == fixture() and "error" not in dev
    assert other == {"name": "aerarium", "error": "unreachable"}
    refused = backend(dev=raising(ClientError("forbidden", 403)))
    teams_ = call(refused, "GET", "/api/status")[1]["teams"]
    assert teams_[0]["error"] == "forbidden" and teams_[1]["view"] == aerarium()


def test_no_token_appears_in_any_output() -> None:
    down = backend(
        aerarium=raising(ClientError(f"Bearer {TOKEN}", 401)),
        third=raising(RuntimeError(TOKEN)),
    )
    outputs = [call(down, "GET", path)[1] for path in ("/", "/dashboard_teams.js", "/api/status")]
    assert all(TOKEN not in json.dumps(o) for o in outputs)
    assert call(down, "GET", "/api/status")[1]["teams"][1]["error"] == "unreachable"
    # The page code has no path for a credential: it reads only /api/status.
    script = (KIT / "dashboard_teams.js").read_text()
    assert script.count("fetch(") == 1 and "token" not in script.lower()


def test_the_page_is_read_only() -> None:
    b = backend()
    for method in ("POST", "PUT", "DELETE"):
        assert call(b, method, "/api/message", {"role": "dev"})[0] in (404, 501)
    page = (KIT / "dashboard_teams.html").read_text()
    assert "<form" not in page and "<button" not in page
    assert "method:" not in (KIT / "dashboard_teams.js").read_text()
    assert call(b, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403


def test_a_non_loopback_bind_is_refused() -> None:
    module = teams()
    for bind in ("0.0.0.0", "192.168.1.5", "", "::"):
        with pytest.raises(module.DashboardError):
            module.server(backend(), 0, bind)


def test_the_cli_refuses_a_non_loopback_bind(tmp_path: Path) -> None:
    module = teams()
    one = tmp_path / "a.json"
    one.write_text(json.dumps(fixture()))
    assert module.main(["--fixtures", f"dev={one}", "--bind", "0.0.0.0"]) == 2


def test_the_teams_file_is_validated(tmp_path: Path) -> None:
    module = teams()
    path = tmp_path / "teams.json"
    path.write_text(json.dumps({"teams": [{"name": "dev", "host": "d.json"}]}))
    assert module.read_teams(path) == [("dev", tmp_path / "d.json")]
    bads: list[Any] = [
        {"teams": []},
        {"teams": [{"name": "dev", "host": "d.json", "token": TOKEN}]},
        {"teams": [{"name": "a b", "host": "d.json"}]},
        {"teams": [{"name": "dev", "host": "x"}, {"name": "dev", "host": "y"}]},
    ]
    for bad in bads:
        path.write_text(json.dumps(bad))
        with pytest.raises(module.DashboardError):
            module.read_teams(path)
