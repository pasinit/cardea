"""The harness contract (ADR-033): typed request and result, and the first two adapters."""

import asyncio
import io
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_confined import WorkerHost, finished

from cardea.hosts import session_worker
from cardea.runtime.agent_session import ClaudeAgentSession, SessionResult
from cardea.runtime.harness import HarnessRequest, HarnessResult
from cardea.service.confined import ConfinedAgentSession, Workers


def request(**changes: object) -> HarnessRequest:
    fields: dict[str, object] = {
        "system_prompt": "system",
        "prompt": "do it",
        "mcp_url": "http://127.0.0.1:4100/mcp",
        "mcp_token": "token",
        "tool_names": ("pages-fetch",),
        "max_turns": 4,
        "max_cost_usd": 0.5,
        "timeout_seconds": 30.0,
        "job_key": "task-1:1",
        **changes,
    }
    return HarnessRequest.model_validate(fields)


def test_result_requires_a_reason_exactly_when_it_failed() -> None:
    assert HarnessResult(text="ok", success=True).failure_reason is None
    with pytest.raises(ValidationError):
        HarnessResult(text="", success=False)
    with pytest.raises(ValidationError):
        HarnessResult(text="ok", success=True, failure_reason="timeout")
    failed = HarnessResult.failed("attempt_timeout")
    assert failed.is_error and failed.failure_reason == failed.stop_reason == "attempt_timeout"


def test_legacy_fields_map_onto_the_contract() -> None:
    legacy = SessionResult(text="", is_error=True, stop_reason="error_max_turns")
    assert legacy == HarnessResult.failed("error_max_turns")
    assert HarnessResult.model_validate({"text": "x", "is_error": True}).failure_reason == "error"
    assert SessionResult(text="done", is_error=False, cost_usd=0.1) == HarnessResult(
        text="done", success=True, cost_usd=0.1
    )


def test_request_rejects_unknown_fields_and_nonpositive_bounds() -> None:
    with pytest.raises(ValidationError):
        request(credential="/etc/secret")
    with pytest.raises(ValidationError):
        request(max_turns=0)


def test_claude_harness_takes_the_request_model_over_its_default(tmp_path: Path) -> None:
    adapter = ClaudeAgentSession(model="default-model", api_key="k")
    assert adapter.options(request(), tmp_path)["model"] == "default-model"
    assert adapter.options(request(model="other-model"), tmp_path)["model"] == "other-model"


def test_confined_harness_passes_the_request_model_and_no_credential() -> None:
    host = WorkerHost()
    host.outcome = finished({"output": {"text": "done", "success": True, "turns": 2}})
    session = ConfinedAgentSession(Workers(host), "session", "agent", "default-model")
    result = asyncio.run(session.run(request(model="other-model")))
    assert result == HarnessResult(text="done", success=True, turns=2)
    (started,) = host.starts
    assert started["payload"]["model"] == "other-model"
    assert "model" not in started["payload"]["request"]
    assert "credential" not in json.dumps(started["payload"]).lower()


def test_confined_harness_refuses_malformed_worker_output() -> None:
    host = WorkerHost()
    host.outcome = finished({"output": {"text": "done", "success": True, "turns": "many"}})
    session = ConfinedAgentSession(Workers(host), "session", "agent", None)
    result = asyncio.run(session.run(request()))
    assert result.failure_reason == "worker_output_invalid"


def test_session_worker_speaks_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("key\n")
    monkeypatch.setenv("CARDEA_CREDENTIAL", str(credential))
    monkeypatch.setenv("CARDEA_CREDENTIAL_KIND", "api_key")
    seen: list[tuple[str | None, HarnessRequest]] = []

    async def run(self: ClaudeAgentSession, received: HarnessRequest) -> HarnessResult:
        seen.append((self.model, received))
        return HarnessResult(text="done", success=True, turns=1, cost_usd=0.01)

    monkeypatch.setattr(ClaudeAgentSession, "run", run)
    document = {"model": "m", "request": request().model_dump(mode="json", exclude={"model"})}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(document).encode())))
    assert session_worker.main() == 0
    output = json.loads(capsys.readouterr().out)["output"]
    assert HarnessResult.model_validate(output) == HarnessResult(
        text="done", success=True, turns=1, cost_usd=0.01
    )
    assert seen == [("m", request())]
