"""Executed native SDK fit; no live credential, network provider or Debian claim."""

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.server import MCPServer
from mcp.types import Tool

from cardea.runtime.incident_tool import server
from cardea.runtime.incidents import IncidentArtifacts, IncidentBatch, verify
from cardea.runtime.mcp_tools import MCPTools, ToolFailure
from cardea.runtime.openrouter import ModelFailure, OpenRouterModel

SCHEMA = json.loads(
    (Path(__file__).parents[2] / "src/cardea/runtime/incident-tool-schema.json").read_text()
)
TOOL = Tool(name="compile_incidents", input_schema=SCHEMA)
INPUT = {"batch": {"records": [{"id": "INC-1", "title": "Fictional outage", "severity": "high"}]}}


def response() -> dict[str, Any]:
    return {
        "id": "gen-fixture",
        "created": 1,
        "model": "fixture/model",
        "object": "chat.completion",
        "system_fingerprint": None,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "logprobs": None,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "compile_incidents",
                                "arguments": json.dumps(INPUT),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 17, "completion_tokens": 23, "total_tokens": 40, "cost": 0.0001},
    }


def propose(payload: dict[str, Any]) -> Any:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    return asyncio.run(
        OpenRouterModel("fictional-key", "fixture/model", transport=transport).propose(
            "Create the incident index", INPUT, [TOOL], 1024
        )
    )


def test_native_sdk_request_and_usage() -> None:
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Accept-Encoding"] == "identity"
        assert body["model"] == "fixture/model"
        assert "parallel_tool_calls" not in body
        assert "max_completion_tokens" not in body
        assert body["max_tokens"] == 1024
        assert body["tool_choice"] == "required"
        assert body["provider"] == {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        }
        assert not {"plugins", "models", "stop_server_tools_when"} & body.keys()
        assert "fictional-key" not in request.content.decode()
        return httpx.Response(200, json=response())

    result = asyncio.run(
        OpenRouterModel(
            "fictional-key", "fixture/model", transport=httpx.MockTransport(send)
        ).propose("Index", INPUT, [TOOL], 1024)
    )
    assert len(calls) == 1
    assert result.arguments == INPUT and result.usage.total_tokens == 40
    assert result.usage.cost == 0.0001
    assert result.response_id == "gen-fixture" and result.call_id == "call-1"


@pytest.mark.parametrize("status", [429, 500, 503, 307])
def test_no_hidden_retry_or_redirect_and_sanitized_failure(status: int) -> None:
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "fictional-key-sensitive"}},
            headers={"Location": "https://unapproved.invalid"},
        )

    with pytest.raises(ModelFailure, match="model_outcome_unknown") as failure:
        asyncio.run(
            OpenRouterModel(
                "fictional-key", "fixture/model", transport=httpx.MockTransport(send)
            ).propose("Index", INPUT, [TOOL], 1024)
        )
    assert len(calls) == 1 and "sensitive" not in str(failure.value)
    assert failure.value.usage is None


@pytest.mark.parametrize(
    "case",
    [
        "missing_usage",
        "negative_usage",
        "inconsistent_usage",
        "parallel",
        "unknown",
        "arguments",
        "schema",
        "truncated",
        "excess_tokens",
        "nan",
    ],
)
def test_invalid_proposals_never_dispatch(case: str) -> None:
    body = response()
    call = body["choices"][0]["message"]["tool_calls"][0]
    if case == "missing_usage":
        del body["usage"]
    elif case == "negative_usage":
        body["usage"]["prompt_tokens"] = -1
    elif case == "inconsistent_usage":
        body["usage"]["total_tokens"] = 1
    elif case == "parallel":
        body["choices"][0]["message"]["tool_calls"].append(copy.deepcopy(call))
    elif case == "unknown":
        call["function"]["name"] = "shell"
    elif case == "arguments":
        call["function"]["arguments"] = "not json"
    elif case == "schema":
        call["function"]["arguments"] = '{"command":"whoami"}'
    elif case == "truncated":
        body["choices"][0]["finish_reason"] = "length"
    elif case == "excess_tokens":
        body["usage"].update(completion_tokens=1025, total_tokens=1042)
    elif case == "nan":
        call["function"]["arguments"] = '{"batch":{"records":NaN}}'
    with pytest.raises(ModelFailure):
        propose(body)


def test_provider_cancellation_propagates_without_retry() -> None:
    async def run() -> None:
        started, stopped = asyncio.Event(), asyncio.Event()
        calls = 0

        async def send(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            raise AssertionError("unreachable")

        task = asyncio.create_task(
            OpenRouterModel(
                "fictional-key", "fixture/model", transport=httpx.MockTransport(send)
            ).propose("Index", INPUT, [TOOL], 1024)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1 and stopped.is_set()

    asyncio.run(run())


def test_context_bound_prevents_network_request() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Network must not be reached")

    with pytest.raises(ModelFailure, match="context_too_large"):
        asyncio.run(
            OpenRouterModel(
                "fictional-key", "fixture/model", transport=httpx.MockTransport(send)
            ).propose("x" * 17000, INPUT, [TOOL], 1024)
        )


def test_response_bound() -> None:
    body = response()
    body["choices"][0]["message"]["reasoning"] = "x" * 140000
    with pytest.raises(ModelFailure, match="response_too_large"):
        propose(body)


@pytest.mark.parametrize("stdio", [False, True])
def test_native_mcp_discovery_call_and_independent_verifier(stdio: bool) -> None:
    async def run() -> None:
        target = (
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "cardea.runtime.incident_tool"],
                env={"PYTHONPATH": str(Path(__file__).parents[2] / "src")},
            )
            if stdio
            else server
        )
        async with Client(target, cache=None, read_timeout_seconds=5) as client:
            assert client.protocol_version == "2026-07-28"
            adapter = MCPTools(client, {"compile_incidents": SCHEMA})
            tools = await adapter.discover()
            result = await adapter.call(tools[0], INPUT)
            artifacts = IncidentArtifacts.model_validate(result.structured_content)
            verify(IncidentBatch.model_validate(INPUT["batch"]), artifacts)
            with pytest.raises(ValueError):
                verify(
                    IncidentBatch.model_validate(INPUT["batch"]),
                    artifacts.model_copy(update={"summary": "Trust me, completed"}),
                )

    asyncio.run(run())


def test_mcp_catalog_change_denied() -> None:
    async def run() -> None:
        async with Client(server) as client:
            with pytest.raises(ToolFailure, match="schema_changed"):
                await MCPTools(client, {"compile_incidents": {"type": "object"}}).discover()

    asyncio.run(run())


def test_mcp_error_cannot_be_success() -> None:
    failing = MCPServer("failing", log_level="ERROR")

    @failing.tool()
    def compile_incidents(batch: IncidentBatch) -> IncidentArtifacts:
        raise ValueError("fixture failure")

    async def run() -> None:
        async with Client(failing) as client:
            adapter = MCPTools(client, {"compile_incidents": SCHEMA})
            tools = await adapter.discover()
            with pytest.raises(ToolFailure, match="tool_failed"):
                await adapter.call(tools[0], INPUT)

    asyncio.run(run())


def test_mcp_call_cancellation_reaches_handler() -> None:
    async def run() -> None:
        started, stopped = asyncio.Event(), asyncio.Event()
        slow = MCPServer("slow", log_level="ERROR")

        @slow.tool()
        async def compile_incidents(batch: IncidentBatch) -> IncidentArtifacts:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            raise AssertionError("unreachable")

        async with Client(slow) as client:
            adapter = MCPTools(client, {"compile_incidents": SCHEMA})
            tools = await adapter.discover()
            task = asyncio.create_task(adapter.call(tools[0], INPUT))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(stopped.wait(), 2)

    asyncio.run(run())


def test_mcp_server_disconnect_is_unknown_and_not_retried(tmp_path: Path) -> None:
    marker = tmp_path / "invocations"
    script = tmp_path / "disconnect.py"
    script.write_text(
        "import os\nfrom pathlib import Path\n"
        "from mcp.server import MCPServer\n"
        "from cardea.runtime.incidents import IncidentBatch, IncidentArtifacts\n"
        "server = MCPServer('disconnect', log_level='ERROR')\n"
        "@server.tool()\n"
        "def compile_incidents(batch: IncidentBatch) -> IncidentArtifacts:\n"
        f"    with Path({str(marker)!r}).open('a') as f: f.write('called\\n')\n"
        "    os._exit(7)\n"
        "server.run(transport='stdio')\n"
    )

    async def run() -> None:
        target = StdioServerParameters(
            command=sys.executable,
            args=[str(script)],
            env={"PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        )
        async with Client(target, cache=None, read_timeout_seconds=3) as client:
            adapter = MCPTools(client, {"compile_incidents": SCHEMA})
            tools = await adapter.discover()
            with pytest.raises(ToolFailure, match="outcome_unknown"):
                await adapter.call(tools[0], INPUT)

    asyncio.run(run())
    assert marker.read_text() == "called\n"
