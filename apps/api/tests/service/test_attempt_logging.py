"""Attempt observability: lifecycle lines, harness output capture, heartbeat, redaction."""

import asyncio
import hashlib
import json
import logging
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_agent_runs as runs
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)
from mcp.client.session import ClientSession
from test_agent_runs import FakeSession, call, routine, submit
from test_confined import WorkerHost
from test_confined import finished as worker_finished
from test_confined_harnesses import SRC, sandbox_env, worker_document
from test_opencode import found, harness, listing_tools, request

from cardea.runtime import activity as activities
from cardea.runtime.activity import AttemptActivity, OutputLimits, scrub
from cardea.runtime.agent_session import SessionRequest, SessionResult, observe
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.confined import ConfinedAgentSession, Workers
from cardea.service.kernel import Kernel
from cardea.service.logs import (
    ATTEMPT_LOGS,
    JournalFormatter,
    configure_logging,
    log_level,
)
from cardea.service.models import AgentTools, AttemptLogging, ServiceConfig
from cardea.service.tool_endpoint import ToolEndpoint

# The agent-run fixtures: a provisioned kernel, scripted session and fake tools.
kernel, session, tools = runs.kernel, runs.session, runs.tools


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture every `cardea.*` record, even after the service configured its handler."""
    root = logging.getLogger("cardea")
    propagate, handlers = root.propagate, root.handlers[:]
    root.propagate, root.handlers[:] = True, []
    caplog.set_level(logging.DEBUG, logger="cardea")
    try:
        yield caplog
    finally:
        root.propagate, root.handlers[:] = propagate, handlers


def journal(caplog: pytest.LogCaptureFixture) -> str:
    """Everything as the service would write it to journald."""
    formatter = JournalFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(message)]


# Lifecycle


def test_an_attempt_logs_its_lifecycle_and_tool_calls(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    session.scripts = [routine]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"

    (started,) = records(logs, "attempt started")
    assert started.__dict__["task_id"] == task_id and started.__dict__["attempt"] == 1
    assert started.__dict__["harness"] == "claude-sdk"
    assert started.__dict__["lease_expires_at"] is not None
    begun = records(logs, "tool call started")
    ended = records(logs, "tool call finished")
    assert [(r.__dict__["operation"], r.__dict__["operation_id"]) for r in begun] == [
        ("listings.search", "call-0001"),
        ("mail.send", "call-0002"),
    ]
    for record in ended:
        assert record.__dict__["task_id"] == task_id
        assert record.__dict__["outcome"] == "ok" and record.levelno == logging.INFO
        assert isinstance(record.__dict__["duration_ms"], int)
    (session_end,) = records(logs, "harness session ended")
    assert session_end.__dict__["outcome"] == "ok" and session_end.__dict__["turns"] == 3
    (finished,) = records(logs, "attempt finished")
    assert finished.__dict__["outcome"] == "succeeded"
    assert finished.__dict__["tool_calls"] == 2
    # Arguments and outputs never reach the log.
    text = journal(logs)
    assert "user@example.invalid" not in text and "3 new listings" not in text
    assert "message_id" not in text and "Search listings" not in text


def test_failed_tool_calls_and_failed_attempts_log_their_codes(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    async def failing(client: ClientSession, req: SessionRequest) -> SessionResult:
        assert (await call(client, "listings-search", to="refuse")).is_error
        return SessionResult(text="", is_error=True, stop_reason="error")

    session.scripts = [failing]
    task_id = submit(kernel, max_attempts=1)
    assert AgentRunRuntime(kernel).execute(task_id) == "failed"
    (ended,) = records(logs, "tool call finished")
    assert ended.levelno == logging.WARNING
    assert ended.__dict__["outcome"] != "ok" and ended.__dict__["reason"] == "rejected"
    (finished,) = records(logs, "attempt finished")
    assert finished.levelno == logging.WARNING
    assert finished.__dict__["outcome"] == "failed"
    assert finished.__dict__["code"] == "session_failed"


def test_a_busy_refusal_is_logged_and_counted_but_not_receipted(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    kernel.agent_tools = AgentTools(max_concurrent_tool_calls=1)

    async def eager(client: ClientSession, req: SessionRequest) -> SessionResult:
        held = asyncio.ensure_future(call(client, "listings-search", to="hold"))
        await asyncio.sleep(0.2)
        assert "tool_busy" in str((await call(client, "mail-send", to="x@example.invalid")).content)
        assert not (await held).is_error
        return SessionResult(text="Searched.", is_error=False)

    session.scripts = [eager]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    (refused,) = records(logs, "tool call refused")
    assert refused.levelno == logging.WARNING and refused.__dict__["task_id"] == task_id
    assert refused.__dict__["code"] == "tool_busy" and refused.__dict__["operation"] == "mail.send"
    assert refused.__dict__["in_flight"] == 1
    assert [r.__dict__["operation"] for r in records(logs, "tool call started")] == [
        "listings.search"
    ]
    (finished,) = records(logs, "attempt finished")
    assert finished.__dict__["tool_calls"] == 1 and finished.__dict__["tool_calls_refused"] == 1
    (starting,) = records(logs, "harness session starting")
    assert starting.__dict__["max_concurrent_tool_calls"] == 1
    assert starting.__dict__["tool_timeout_s"] > 0


# Secrets


def test_the_endpoint_token_and_credentials_never_reach_the_log(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    async def leaky(client: ClientSession, req: SessionRequest) -> SessionResult:
        # A harness echoing its configuration to stderr, as some CLIs do on errors.
        output = activities.current()
        output.harness_output(f"connecting to {req.mcp_url} with token {req.mcp_token}")
        output.harness_output(f"Authorization: Bearer {req.mcp_token}")
        output.harness_output('request failed: {"api_key": "sk-live-abcdefghijkl"}')
        await call(client, "listings-search")
        return SessionResult(text="done", is_error=False)

    session.scripts = [leaky]
    assert AgentRunRuntime(kernel).execute(submit(kernel)) == "succeeded"
    token = session.requests[0].mcp_token
    text = journal(logs) + logs.text
    assert len(token) > 20 and token not in text
    assert "sk-live-abcdefghijkl" not in text
    assert text.count("[redacted]") >= 3
    assert len(records(logs, "harness stderr: connecting to")) == 1


def test_scrub_removes_registered_and_credential_shaped_values() -> None:
    assert scrub("x=abc secretvalue123 y", ("secretvalue123",)) == "x=abc [redacted] y"
    assert scrub("Authorization: Bearer abc.def") == "Authorization: Bearer [redacted]"
    assert scrub("bearer abc.def") == "bearer [redacted]"
    assert scrub("password=hunter22 next") == "password=[redacted] next"
    assert scrub("key sk-abcdefgh12345") == "key [redacted]"
    assert scrub("ordinary text, 3 listings") == "ordinary text, 3 listings"


def test_sdk_messages_are_logged_as_metadata_only(logs: pytest.LogCaptureFixture) -> None:
    activity = AttemptActivity("task-x", 1, harness="claude-sdk")
    observe(activity, SystemMessage("init", {"mcp_servers": [{"name": "cardea", "status": "ok"}]}))
    observe(
        activity,
        AssistantMessage(
            content=[
                TextBlock(text="private completion text"),
                ToolUseBlock(id="t1", name="mcp__cardea__mail-send", input={"body": "private"}),
            ],
            model="m",
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
    )
    observe(
        activity,
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="s",
            result="private result text",
            api_error_status=529,
        ),
    )
    (turn,) = records(logs, "model turn")
    assert turn.__dict__["tools"] == ["mcp__cardea__mail-send"]
    assert turn.__dict__["blocks"] == "TextBlock,ToolUseBlock"
    assert turn.__dict__["input_tokens"] == 10
    (result,) = records(logs, "model result")
    assert result.levelno == logging.WARNING and result.__dict__["code"] == 529
    (init,) = records(logs, "sdk system message: init")
    assert init.__dict__["mcp"] == ["cardea:ok"]
    assert "private" not in journal(logs)
    assert activity.turns == 1 and activity.last_model is not None


# Harness output bounds


def test_harness_output_is_truncated_per_line_and_capped_per_attempt(
    logs: pytest.LogCaptureFixture,
) -> None:
    activity = AttemptActivity("task-x", 1, limits=OutputLimits(line_bytes=16, total_bytes=36))
    activity.harness_output(b"short line\n")
    activity.harness_output("x" * 100)
    activity.harness_output("   \n")  # blank lines are skipped
    activity.harness_output("third line")
    activity.harness_output("dropped line")
    lines = records(logs, "harness stderr")
    assert [r.getMessage() for r in lines] == [
        "harness stderr: short line",
        "harness stderr: " + "x" * 16 + " ...[truncated 84 bytes]",
        "harness stderr: third line",
    ]
    assert all(r.__dict__["task_id"] == "task-x" for r in lines)
    (cap,) = records(logs, "harness output cap reached")
    assert cap.levelno == logging.WARNING
    assert activity.summary()["output_lines_dropped"] == 1


FAKE_OPENCODE = """#!{python}
import json, os, sys
sys.stdin.read()
sys.stderr.write("INFO service=mcp key=cardea status=connected\\n")
sys.stderr.write("WARN provider said: " + "y" * 5000 + "\\n")
sys.stderr.write("WARN retrying with key " + os.environ["CARDEA_PROVIDER_KEY"] + "\\n")
sys.stderr.flush()
def emit(kind, part):
    print(json.dumps({{"type": kind, "timestamp": 1, "sessionID": "s", "part": part}}))
emit("step_start", {{}})
emit("text", {{"text": "private completion"}})
emit("step_finish", {{"tokens": {{"input": 100, "output": 10, "reasoning": 0,
     "cache": {{"read": 0, "write": 0}}}}, "cost": 0.0, "reason": "stop"}})
"""


def test_opencode_stderr_and_steps_are_logged_with_the_attempt(
    tmp_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    binary = tmp_path / "opencode"
    binary.write_text(FAKE_OPENCODE.format(python=sys.executable))
    binary.chmod(0o755)
    activity = AttemptActivity("task-oc", 2, harness="glm", limits=OutputLimits(line_bytes=512))

    async def run(url: str, token: str) -> Any:
        with activities.bind(activity):
            return await harness(binary=binary).run(request(mcp_url=url, mcp_token=token))

    with ToolEndpoint(listing_tools(), found) as endpoint:
        result = asyncio.run(run(endpoint.url, endpoint.token))
        token = endpoint.token
    assert result.success and result.text == "private completion"

    (spawned,) = records(logs, "harness process started")
    assert spawned.__dict__["task_id"] == "task-oc" and spawned.__dict__["attempt"] == 2
    assert isinstance(spawned.__dict__["pid"], int) and spawned.__dict__["argv"][1] == "run"
    stderr = [r.getMessage() for r in records(logs, "harness stderr")]
    assert stderr[0] == "harness stderr: INFO service=mcp key=cardea status=connected"
    assert stderr[1].endswith(f"...[truncated {len('WARN provider said: ') + 5000 - 512} bytes]")
    assert stderr[2] == "harness stderr: WARN retrying with key [redacted]"
    assert [r.__dict__["step"] for r in records(logs, "model ")] == ["request", "turn"]
    (exited,) = records(logs, "harness process exited")
    assert exited.__dict__["returncode"] == 0 and exited.levelno == logging.INFO
    text = journal(logs)
    assert "sk-provider-secret" not in text and token not in text
    assert "private completion" not in text


# Heartbeat


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_heartbeat_reports_time_since_model_and_tool_activity(
    logs: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    activity = AttemptActivity("task-hb", 1, heartbeat_seconds=60, clock=clock)
    clock.now += 10
    activity.model_activity("turn")
    clock.now += 5
    activity.tool_started("mail.send", "call-0001")
    clock.now += 44
    assert not activity.tick()  # 59 s: not yet
    clock.now += 1
    assert activity.tick()
    assert not activity.tick()  # the next one is 60 s later
    (beat,) = records(logs, "attempt still running")
    fields = beat.__dict__
    assert fields["elapsed_s"] == 60.0
    assert fields["since_model_s"] == 50.0 and fields["since_tool_s"] == 45.0
    assert fields["tools_in_flight"] == 1 and fields["turns"] == 1
    assert "since_output_s" not in fields  # no harness output yet: never
    clock.now += 60
    assert activity.tick()
    assert len(records(logs, "attempt still running")) == 2


def test_heartbeat_zero_disables_it() -> None:
    clock = Clock()
    activity = AttemptActivity("task-hb", 1, heartbeat_seconds=0, clock=clock)
    clock.now += 3600
    assert not activity.tick()


def test_a_running_attempt_emits_heartbeats(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    async def slow(client: ClientSession, req: SessionRequest) -> SessionResult:
        await call(client, "listings-search")
        await asyncio.sleep(2.3)
        return SessionResult(text="done", is_error=False)

    kernel.attempt_logging = AttemptLogging(heartbeat_seconds=1)
    session.scripts = [slow]
    task_id = submit(kernel)
    assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
    beats = records(logs, "attempt still running")
    assert len(beats) >= 2
    assert all(b.__dict__["task_id"] == task_id for b in beats)
    assert beats[-1].__dict__["since_tool_s"] >= 1.0
    assert beats[-1].__dict__["tools_in_flight"] == 0


# Per-attempt files


def test_attempt_log_files_are_private_and_bounded(
    kernel: Kernel, session: FakeSession, logs: pytest.LogCaptureFixture
) -> None:
    kernel.attempt_logging = AttemptLogging(files=True, files_retained=2)
    task_ids = []
    for index in range(3):
        session.scripts = [routine]
        task_id = submit(kernel, request_id=f"routine-{index}")
        assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
        task_ids.append(task_id)
    directory = kernel.database.directory / ATTEMPT_LOGS
    files = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime)
    assert [p.name for p in files] == [f"{t}-1.log" for t in task_ids[1:]]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    lines = [json.loads(line) for line in files[-1].read_text().splitlines()]
    assert {line["task_id"] for line in lines} == {task_ids[-1]}
    messages = [line["msg"] for line in lines]
    assert messages[0] == "attempt started" and messages[-1] == "attempt finished"
    assert "tool call finished" in messages and all("ts" in line for line in lines)
    token = session.requests[-1].mcp_token
    assert token not in files[-1].read_text()


# Configuration


def test_service_config_has_logging_defaults() -> None:
    config = ServiceConfig.model_validate(
        {
            "data_dir": "/var/lib/cardea",
            "spiffe_socket": "unix:///run/spire/agent.sock",
            "trust_domain": "example.invalid",
            "runtime_spiffe_id": "spiffe://example.invalid/runtime",
        }
    )
    assert config.log_level == "INFO"
    settings = config.attempt_logging
    assert (settings.heartbeat_seconds, settings.files, settings.files_retained) == (60, False, 200)
    assert (settings.output_line_bytes, settings.output_max_bytes) == (2048, 262144)
    with pytest.raises(ValueError):
        ServiceConfig.model_validate({**config.model_dump(mode="json"), "log_level": "LOUD"})


def test_configured_level_applies_unless_the_environment_overrides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger("cardea")
    saved = root.handlers[:], root.level, root.propagate
    try:
        monkeypatch.delenv("CARDEA_LOG_LEVEL", raising=False)
        configure_logging("WARNING")
        assert root.level == logging.WARNING and not root.propagate
        assert isinstance(root.handlers[0].formatter, JournalFormatter)
        monkeypatch.setenv("CARDEA_LOG_LEVEL", "debug")
        assert log_level("WARNING") == "DEBUG"
        monkeypatch.setenv("CARDEA_LOG_LEVEL", "nonsense")
        assert log_level("ERROR") == "ERROR"
    finally:
        root.handlers[:], root.level, root.propagate = saved[0], saved[1], saved[2]


def test_journal_lines_carry_attempt_fields() -> None:
    record = logging.LogRecord("cardea.attempt", logging.INFO, __file__, 1, "x", None, None)
    record.__dict__.update({"task_id": "t", "attempt": 2, "secret_field": "nope"})
    entry = json.loads(JournalFormatter().format(record))
    assert entry["task_id"] == "t" and entry["attempt"] == 2 and "secret_field" not in entry
    assert "ts" in json.loads(JournalFormatter(timestamps=True).format(record))


# Confined sessions


def test_a_confined_session_logs_its_worker_unit(logs: pytest.LogCaptureFixture) -> None:
    host = WorkerHost()
    host.installed["workers"]["opencode"] = ["session"]
    host.outcome = worker_finished({"output": {"text": "done", "success": True, "turns": 2}})
    adapter = ConfinedAgentSession(Workers(host), "session", "opencode", "z-ai/glm-4.6")
    activity = AttemptActivity("task-c", 1)

    async def run() -> Any:
        with activities.bind(activity):
            return await adapter.run(request(job_key="task-c:1"))

    assert asyncio.run(run()).success
    (started,) = records(logs, "confined session starting")
    job = adapter.job("task-c:1")
    assert started.__dict__["unit"] == f"cardea-host-{job}.service"
    assert started.__dict__["task_id"] == "task-c"
    assert len(records(logs, "confined session finished")) == 1


def test_the_session_worker_writes_nothing_to_stderr(tmp_path: Path) -> None:
    # The unit duplicates the result file onto stderr: a log line there would corrupt it.
    binary = tmp_path / "opencode"
    binary.write_text(FAKE_OPENCODE.format(python=sys.executable) + "sys.exit(3)\n")
    binary.chmod(0o755)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with ToolEndpoint(listing_tools(), found) as endpoint:
        document = worker_document(digest, endpoint.url)
        document["request"]["mcp_token"] = endpoint.token
        done = subprocess.run(
            [sys.executable, "-m", "cardea.hosts.session_worker"],
            env={**sandbox_env(tmp_path, binary, None), "PYTHONPATH": str(SRC)},
            input=json.dumps(document).encode(),
            capture_output=True,
            timeout=120,
            check=True,
        )
    assert done.stderr == b""
    assert json.loads(done.stdout)["output"]["failure_reason"] == "harness_error"
