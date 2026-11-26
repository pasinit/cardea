"""The dashboard's team-wide Pause / Resume (deploy/team-kit/dashboard_teams.py `control`):
nothing happens without confirmation, the kernel's live read decides who may pause, a paused
team can be resumed although its supervisor is disabled, and every attempt is audited."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_dashboard import KIT
from test_dashboard_teams import call, teams

__all__ = []


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled: bool = False) -> Any:
    log: list[Any] = []
    kit = SimpleNamespace(
        load_host=lambda path: {"path": path},
        pause=lambda host, reason: log.append(("pause", reason)),
        resume=lambda host: log.append(("resume",)),
    )
    applier = SimpleNamespace(audit_path=lambda kit, host: tmp_path / "audit.jsonl")
    module = teams()
    monkeypatch.setattr(module, "apply_modules", lambda: (kit, None, applier))

    def read() -> dict[str, Any]:
        if disabled:
            raise Refused("principal_disabled")
        return {"group": "dev-team"}

    backend = module.TeamsBackend({"dev": read}, hosts={"dev": tmp_path / "host.json"})
    backend.log, backend.audit = log, tmp_path / "audit.jsonl"
    return backend


def control(backend: Any, **body: Any) -> tuple[int, Any]:
    return call(backend, "POST", "/api/control", {"team": "dev", "confirm": True, **body})


@pytest.mark.loopback
def test_pause_needs_confirmation_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make(tmp_path, monkeypatch)
    status, body = call(backend, "POST", "/api/control", {"team": "dev", "action": "pause"})
    assert (status, body["code"]) == (400, "invalid_control")
    status, body = control(backend, action="pause", confirm=False)
    assert status == 400 and backend.log == []
    status, body = control(backend, action="pause", reason="owner asked")
    assert (status, body) == (200, {"status": "paused"})
    assert backend.log == [("pause", "owner asked")]
    assert backend.audit.read_text().count('"outcome"') == 2


@pytest.mark.loopback
def test_a_paused_team_can_be_resumed_and_a_running_one_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = make(tmp_path, monkeypatch, disabled=True)
    assert control(paused, action="resume") == (200, {"status": "resumed"})
    assert paused.log == [("resume",)]
    status, body = control(paused, action="pause")
    assert (status, body["code"]) == (409, "already_paused")
    running = make(tmp_path, monkeypatch)
    status, body = control(running, action="resume")
    assert (status, body["code"]) == (409, "not_paused")


@pytest.mark.loopback
def test_the_kernels_refusal_and_unknown_input_stop_the_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make(tmp_path, monkeypatch)

    def refuse() -> dict[str, Any]:
        raise Refused("forbidden")

    backend.sources["dev"] = refuse
    status, body = control(backend, action="pause")
    assert status == 502 and backend.log == [] and not backend.audit.exists()
    backend = make(tmp_path, monkeypatch)
    assert control(backend, action="stop")[0] == 400
    assert control(backend, action="pause", team="nope")[0] == 404
    extra = {"team": "dev", "action": "pause", "confirm": True, "x": 1}
    assert call(backend, "POST", "/api/control", extra)[0] == 400


@pytest.mark.loopback
def test_without_a_host_file_there_are_no_controls(tmp_path: Path) -> None:
    backend = teams().TeamsBackend({"dev": lambda: {"group": "g"}})
    assert control(backend, action="pause")[0] == 404
    assert call(backend, "GET", "/api/status")[1]["teams"][0]["controls"] is False
    script = (KIT / "dashboard_teams.js").read_text()
    assert "alertdialog" in script and "confirm: true" in script
