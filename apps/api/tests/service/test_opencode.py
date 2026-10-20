"""The OpenCode harness (ADR-033).

Unit tests run everywhere. End-to-end tests run the real pinned OpenCode binary against a
fake OpenAI-compatible model server and Cardea's real per-attempt MCP endpoint; no real
provider is ever called. They need `CARDEA_TEST_OPENCODE` (the binary, as fetched by
`packaging/fetch_opencode.py`); CI sets `CARDEA_REQUIRE_OPENCODE=1` so they cannot skip.
"""

import asyncio
import json
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from fake_model import FakeModel, Reply
from mcp import types
from mcp.client.session import ClientSession
from test_agent_runs import GRANTS, FakeSession, Tools, descriptor
from test_operations import Auth, FakeClient

from cardea.kernel.models import OperationState
from cardea.runtime.agent_session import SessionRequest, SessionResult
from cardea.runtime.harness import HarnessRequest
from cardea.runtime.opencode import EVENT_TYPES, Meter, OpenCodeHarness, Price
from cardea.service import agent_run_runtime
from cardea.service import kernel as handlers
from cardea.service.agent_run_runtime import AgentRunRuntime
from cardea.service.auth import Authentication
from cardea.service.database import Database, borrow
from cardea.service.harnesses import Catalog, HarnessEntry, Harnesses
from cardea.service.kernel import Kernel, provision
from cardea.service.models import AgentRunRequest, Permission, Principal, Provisioning, now
from cardea.service.operations import Registry
from cardea.service.tool_endpoint import ToolEndpoint

TRANSCRIPT = Path(__file__).parent / "data" / "opencode-1.18.35-run.ndjson"
BINARY = os.environ.get("CARDEA_TEST_OPENCODE")
needs_binary = pytest.mark.skipif(
    not BINARY and os.environ.get("CARDEA_REQUIRE_OPENCODE") != "1",
    reason="set CARDEA_TEST_OPENCODE to the pinned OpenCode binary",
)
PRICES = {"z-ai/glm-4.6": Price(0.5, 2.0), "qwen3:8b": Price(0.0, 0.0)}


def request(**changes: Any) -> HarnessRequest:
    fields: dict[str, Any] = {
        "system_prompt": "You are carrying out one task inside Cardea.",
        "prompt": "Search listings, then report.",
        "mcp_url": "http://127.0.0.1:1/mcp",
        "mcp_token": "endpoint-token",
        "tool_names": ("listings-search",),
        "max_turns": 10,
        "max_cost_usd": 1.0,
        "timeout_seconds": 60.0,
        "model": "z-ai/glm-4.6",
        **changes,
    }
    return HarnessRequest.model_validate(fields)


def harness(base_url: str = "http://127.0.0.1:1/v1", **changes: Any) -> OpenCodeHarness:
    fields: dict[str, Any] = {
        "binary": Path(BINARY or "/nonexistent/opencode"),
        "base_url": base_url,
        "default_model": "z-ai/glm-4.6",
        "prices": PRICES,
        "api_key": "sk-provider-secret",
        **changes,
    }
    return OpenCodeHarness(**fields)


# Event schema contract (pinned version)


def test_recorded_events_match_the_schema_the_adapter_relies_on() -> None:
    events = [json.loads(line) for line in TRANSCRIPT.read_text().splitlines()]
    for event in events:
        assert event["type"] in EVENT_TYPES
        assert isinstance(event["timestamp"], int) and isinstance(event["sessionID"], str)
        part = event["part"]
        if event["type"] == "step_finish":
            tokens = part["tokens"]
            for key in ("input", "output", "reasoning"):
                assert isinstance(tokens[key], int)
            assert {"read", "write"} <= set(tokens["cache"])
            assert isinstance(part["cost"], int | float) and isinstance(part["reason"], str)
        if event["type"] == "tool_use":
            assert part["tool"].startswith("cardea_") and part["state"]["status"] == "completed"
        if event["type"] == "text":
            assert isinstance(part["text"], str)
    meter = Meter(Price(0.5, 2.0))
    for event in events:
        meter.record(event)
    assert (meter.steps, meter.input, meter.output, meter.last_reason) == (2, 2000, 200, "stop")
    assert meter.tools == ["cardea_listings-search"] and meter.texts == ["Found 3 listings."]
    assert meter.cost == pytest.approx((2000 * 0.5 + 200 * 2.0) / 1e6)


def test_a_renamed_field_fails_loudly() -> None:
    event = json.loads(TRANSCRIPT.read_text().splitlines()[2])
    assert event["type"] == "step_finish"
    del event["part"]["tokens"]["input"]
    with pytest.raises(KeyError):
        Meter(Price(1, 1)).record(event)


# Configuration


def test_config_offers_only_cardea_tools_and_holds_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "service-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    adapter = harness(headers={"X-Title": "cardea"})
    config = adapter.config(request(), "z-ai/glm-4.6")
    deny = {"*": "deny", "cardea_*": "allow"}
    assert config["permission"] == deny and config["agent"]["cardea"]["permission"] == deny
    assert config["snapshot"] is False and config["share"] == "disabled"
    assert config["enabled_providers"] == ["cardea-provider"]
    provider = config["provider"]["cardea-provider"]
    assert provider["options"]["apiKey"] == "{env:CARDEA_PROVIDER_KEY}"
    assert provider["options"]["headers"] == {"X-Title": "cardea"}
    assert provider["models"]["z-ai/glm-4.6"]["cost"] == {"input": 0.5, "output": 2.0}
    server = config["mcp"]["cardea"]
    assert server["headers"] == {"Authorization": "Bearer {env:CARDEA_MCP_TOKEN}"}
    text = json.dumps(config)
    assert "sk-provider-secret" not in text and "endpoint-token" not in text
    env = adapter.environment(request(), "z-ai/glm-4.6", tmp_path)
    assert env["CARDEA_PROVIDER_KEY"] == "sk-provider-secret"
    assert env["CARDEA_MCP_TOKEN"] == "endpoint-token"
    assert "SMTP_PASSWORD" not in env and "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["HOME"] == str(tmp_path) and env["OPENCODE_DB"].startswith(str(tmp_path))
    for name in ("AUTOUPDATE", "MODELS_FETCH", "SHARE", "PROJECT_CONFIG", "CLAUDE_CODE"):
        assert env[f"OPENCODE_DISABLE_{name}"] == "1"
    assert {"--pure", "--title"} <= set(adapter.command("z-ai/glm-4.6"))


def test_registry_pins_opencode_as_a_binary() -> None:
    with pytest.raises(ValueError, match="binary pinned by hash"):
        HarnessEntry.model_validate(
            {
                "id": "glm",
                "kind": "opencode",
                "package": {"name": "opencode-ai", "version": "1.18.35"},
                "provider": {"base_url": "https://openrouter.ai/api/v1"},
                "default_model": "z-ai/glm-4.6",
                "models": {
                    "z-ai/glm-4.6": {"price": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 1}}
                },
            }
        )


def test_unreachable_tools_fail_before_the_harness_starts() -> None:
    result = asyncio.run(harness().run(request()))
    assert result.failure_reason == "harness_tools_unavailable" and result.turns == 0


# The runtime requires that a tool-listing harness listed its tools


class ToollessSession(FakeSession):
    requires_tool_listing = True


def runtime_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: Any) -> Kernel:
    monkeypatch.setattr(handlers, "DBOSClient", FakeClient)
    monkeypatch.setattr(agent_run_runtime, "POLL_SECONDS", 0.05)
    tools = Tools()
    registry = Registry(
        (
            descriptor("mail.send", "effect", idempotency_scope=["/to"]),
            descriptor("listings.search", "read"),
        ),
        {"test.mail.send": tools.send, "test.listings.search": tools.search},
    )
    db = Database(tmp_path / "data")
    db.migrate()
    principals = tuple(
        Principal(
            principal_id=p,
            kind="service" if p == "runtime" else "human",
            auth_kind="spiffe",
            issuer="test",
            subject=p,
        )
        for p in ("runtime", "supervisor")
    ) + (Principal(principal_id="agent", kind="agent", runtime_id="runtime"),)
    granted = tuple(
        Permission(
            principal_id="supervisor",
            namespace="test",
            operation=right,
            delegate=True,
            expires_at=now() + timedelta(hours=1),
        )
        for right in (*GRANTS, "harness:glm")
    )
    entry = HarnessEntry.model_validate(
        {
            "id": "glm",
            "kind": "opencode",
            "package": {
                "name": "opencode-ai",
                "version": "1.18.35",
                "binary": "/opt/opencode",
                "sha256": "0" * 64,
            },
            "provider": {"base_url": "http://127.0.0.1:1/v1"},
            "default_model": "z-ai/glm-4.6",
            "models": {
                "z-ai/glm-4.6": {"price": {"input_usd_per_mtok": 0.5, "output_usd_per_mtok": 2}}
            },
        }
    )
    installed = Harnesses({"glm": adapter}, Catalog({"glm": entry}, "glm"))
    provision(
        db,
        Provisioning(
            principals=principals,
            permissions=granted,
            platform=tuple(p.model_copy(update={"principal_id": "platform"}) for p in granted),
        ),
        registry,
        harnesses=installed,
    )
    return Kernel(db, cast(Authentication, Auth()), operations=registry, harnesses=installed)


def run_request(**changes: Any) -> AgentRunRequest:
    return AgentRunRequest.model_validate(
        {
            "request_id": "routine-0830",
            "agent_id": "agent",
            "namespace": "test",
            "objective": "Daily listings recap",
            "instructions": "Search listings, then email the user one recap.",
            "operations": ["listings.search", "mail.send"],
            "grants": [*GRANTS, "harness:glm"],
            "harness": "glm",
            "attempt_timeout_seconds": 60,
            "max_attempts": 1,
            **changes,
        }
    )


def failure(kernel: Kernel, task_id: str) -> str:
    with kernel.database.engine.begin() as conn, borrow(conn) as tx:
        task = tx.get_task(task_id)
    assert task.failure is not None
    return task.failure.message


def test_a_harness_that_never_listed_its_tools_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def toolless(client: ClientSession, received: SessionRequest) -> SessionResult:
        return SessionResult(text="All done, no tools needed.", is_error=False)

    session = ToollessSession()
    session.scripts = [toolless]
    kernel = runtime_kernel(tmp_path, monkeypatch, session)
    try:
        task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
        assert AgentRunRuntime(kernel).execute(task_id) == "failed"
        assert "harness_tools_unavailable" in failure(kernel, task_id)
    finally:
        kernel.database.close()


# End to end with the pinned binary


def listing_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="listings-search",
            description="Search listings",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
    ]


async def found(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text='{"listings": 3}')])


@pytest.fixture
def binary() -> Path:
    assert BINARY, "CARDEA_REQUIRE_OPENCODE=1 but CARDEA_TEST_OPENCODE is not set"
    return Path(BINARY)


@needs_binary
def test_a_run_on_opencode_calls_installed_operations_with_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: Path
) -> None:
    replies = [
        Reply(tool_calls=[("cardea_listings-search", {})]),
        Reply(tool_calls=[("cardea_mail-send", {"to": "user@example.invalid", "body": "3"})]),
        Reply(text="Sent the recap."),
    ]
    with FakeModel(replies) as model:
        adapter = harness(model.base_url, binary=binary)
        kernel = runtime_kernel(tmp_path, monkeypatch, adapter)
        try:
            task_id = kernel.submit(kernel.auth.validate("supervisor"), run_request()).task.task_id
            assert AgentRunRuntime(kernel).execute(task_id) == "succeeded"
            with kernel.database.engine.begin() as conn, borrow(conn) as tx:
                receipts = tx.list_operations(task_id)
                task = tx.get_task(task_id)
                assert task.verification is not None
                output = json.loads(tx.read_object(task.verification.artifacts[0]))
        finally:
            kernel.database.close()
    assert [(r.request.name, r.state) for r in receipts] == [
        ("listings.search", OperationState.SUCCEEDED),
        ("mail.send", OperationState.SUCCEEDED),
    ]
    assert output["text"] == "Sent the recap." and output["harness"] == "glm"
    assert output["turns"] == 3
    assert output["cost_usd"] == pytest.approx(3 * (1000 * 0.5 + 100 * 2.0) / 1e6)
    assert output["usage"]["input_tokens"] == 3000
    # The model was offered Cardea's tools and nothing else, on every call.
    for sent in model.requests:
        assert {t["function"]["name"] for t in sent["tools"]} == {
            "cardea_listings-search",
            "cardea_mail-send",
        }
    assert {h["authorization"] for h in model.headers} == {"Bearer sk-provider-secret"}


@needs_binary
def test_builtin_tools_are_not_available(tmp_path: Path, binary: Path) -> None:
    marker = tmp_path / "touched"
    replies = [
        Reply(tool_calls=[("bash", {"command": f"touch {marker}", "description": "x"})]),
        Reply(tool_calls=[("write", {"filePath": str(marker), "content": "x"})]),
        Reply(text="Could not."),
    ]
    with FakeModel(replies) as model, ToolEndpoint(listing_tools(), found) as endpoint:
        result = asyncio.run(
            harness(model.base_url, binary=binary).run(
                request(mcp_url=endpoint.url, mcp_token=endpoint.token)
            )
        )
    assert not marker.exists()
    assert result.failure_reason in {None, "harness_tool_violation"}
    for sent in model.requests:
        assert all(t["function"]["name"].startswith("cardea_") for t in sent["tools"])


@needs_binary
def test_cardea_kills_the_harness_at_max_turns(binary: Path) -> None:
    with (
        FakeModel([Reply(tool_calls=[("cardea_listings-search", {})])], repeat=True) as model,
        ToolEndpoint(listing_tools(), found) as endpoint,
    ):
        result = asyncio.run(
            harness(model.base_url, binary=binary).run(
                request(mcp_url=endpoint.url, mcp_token=endpoint.token, max_turns=2)
            )
        )
    assert result.failure_reason == "max_turns" and result.turns == 2


@needs_binary
def test_cardea_kills_the_harness_past_max_cost(binary: Path) -> None:
    prices = {"z-ai/glm-4.6": Price(400.0, 400.0)}  # 1100 tokens: $0.44 per step
    with (
        FakeModel([Reply(tool_calls=[("cardea_listings-search", {})])], repeat=True) as model,
        ToolEndpoint(listing_tools(), found) as endpoint,
    ):
        result = asyncio.run(
            harness(model.base_url, binary=binary, prices=prices).run(
                request(mcp_url=endpoint.url, mcp_token=endpoint.token, max_cost_usd=0.5)
            )
        )
    assert result.failure_reason == "max_cost" and result.turns == 2
    assert result.cost_usd == pytest.approx(0.88)
    assert result.usage["harness_cost_usd"] == pytest.approx(0.88)


class FlakyMCP:
    """Answers the preflight's initialize, then fails every request: OpenCode's own
    connection fails after Cardea's preflight succeeded."""

    def __enter__(self) -> "FlakyMCP":
        answered = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", "0")))
                if answered.is_set():
                    self.send_response(500)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                answered.set()
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18"}}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@needs_binary
def test_a_failed_mcp_connection_stops_the_run(binary: Path) -> None:
    with (
        FakeModel([Reply(text="I did it without tools.")], repeat=True) as model,
        FlakyMCP() as mcp,
    ):
        result = asyncio.run(harness(model.base_url, binary=binary).run(request(mcp_url=mcp.url)))
    assert result.failure_reason == "harness_tools_unavailable"


@needs_binary
def test_timeout_cancellation_kills_the_process(
    binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def bounded(adapter: OpenCodeHarness, url: str, token: str) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.run(request(mcp_url=url, mcp_token=token)), 3.0)

    with (
        FakeModel([Reply(text="late", delay=30)]) as model,
        ToolEndpoint(listing_tools(), found) as endpoint,
    ):
        asyncio.run(bounded(harness(model.base_url, binary=binary), endpoint.url, endpoint.token))
    (process,) = started
    assert process.returncode is not None
