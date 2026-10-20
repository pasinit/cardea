"""The OpenCode harness: any OpenAI-compatible provider, Cardea's tools only (ADR-033).

OpenCode (MIT, pinned binary) runs the model loop. Cardea does not trust it with any bound:

- tools: every built-in is denied by a wildcard permission, so the model is offered only
  the per-attempt MCP endpoint's tools; a tool event for anything else stops the run;
- turns: Cardea counts `step_finish` events and kills the process at `max_turns`
  (OpenCode's own `steps` limit is soft);
- cost: Cardea meters tokens against the registry price and kills the process past
  `max_cost_usd` (OpenCode's own figure is an estimate, kept in `usage`);
- tools must connect: the endpoint is preflighted, and a run is stopped when OpenCode
  reports the MCP server failed (the runtime also requires that tools were listed);
- isolation: an empty working directory and a private HOME/XDG/database per attempt,
  no inherited environment, no project or user configuration, no plugins, no sharing,
  no model-catalog fetch;
- credentials: the provider key and the endpoint token reach the process only through
  its environment; the configuration refers to them as `{env:...}`.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cardea.runtime.harness import HarnessRequest, HarnessResult

log = logging.getLogger(__name__)

SERVER = "cardea"  # MCP server key; OpenCode names its tools "cardea_<tool>"
PROVIDER = "cardea-provider"
AGENT = "cardea"
KEY_VARIABLE = "CARDEA_PROVIDER_KEY"
TOKEN_VARIABLE = "CARDEA_MCP_TOKEN"
INHERITED = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
DISABLED = (
    "AUTOUPDATE",
    "MODELS_FETCH",
    "SHARE",
    "PROJECT_CONFIG",
    "CLAUDE_CODE",
    "DEFAULT_PLUGINS",
    "LSP_DOWNLOAD",
    "EXTERNAL_SKILLS",
)
# Event fields this adapter relies on, checked by the schema contract test.
EVENT_TYPES = frozenset({"step_start", "text", "reasoning", "tool_use", "step_finish", "error"})
MAX_LINE = 1 << 20
TOOLS_UNAVAILABLE = "harness_tools_unavailable"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float


@dataclass
class Meter:
    """Running totals of one OpenCode run, from its NDJSON events."""

    price: Price
    steps: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    estimate: float = 0.0
    texts: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_reason: str = ""

    @property
    def cost(self) -> float:
        # Cache reads and writes are charged at the input price: never below the truth for
        # providers that discount cache reads, so the cap stays conservative.
        prompt = self.input + self.cache_read + self.cache_write
        completion = self.output + self.reasoning
        return (prompt * self.price.input + completion * self.price.output) / 1_000_000

    def usage(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "input_tokens": self.input,
            "output_tokens": self.output,
            "reasoning_tokens": self.reasoning,
            "cache_read_tokens": self.cache_read,
            "cache_write_tokens": self.cache_write,
            "metered_cost_usd": round(self.cost, 6),
            "harness_cost_usd": round(self.estimate, 6),
        }

    def record(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "step_start":
            self.texts = []
        elif kind == "text":
            self.texts.append(str(part.get("text", "")))
        elif kind == "tool_use":
            self.tools.append(str(part.get("tool", "")))
        elif kind == "step_finish":
            tokens = part["tokens"]
            cache = tokens.get("cache") or {}
            self.steps += 1
            self.input += int(tokens["input"])
            self.output += int(tokens["output"])
            self.reasoning += int(tokens.get("reasoning") or 0)
            self.cache_read += int(cache.get("read") or 0)
            self.cache_write += int(cache.get("write") or 0)
            self.estimate += float(part.get("cost") or 0)
            self.last_reason = str(part.get("reason", ""))
        elif kind == "error":
            error = event.get("error") or {}
            self.errors.append(str(error.get("name", "error"))[:64])


class Stop(Exception):
    """The adapter stops the run: a bound was reached or a rule was broken."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpenCodeHarness:
    """One installed OpenCode harness: a pinned binary bound to one provider."""

    # The runtime fails an attempt whose endpoint never served a tool listing (ADR-033).
    requires_tool_listing = True

    def __init__(
        self,
        *,
        binary: Path,
        base_url: str,
        default_model: str,
        prices: Mapping[str, Price],
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
        kill_grace: float = 2.0,
    ) -> None:
        self.binary = binary
        self.base_url = base_url
        self.default_model = default_model
        self.prices = dict(prices)
        self.api_key = api_key
        self.headers = dict(headers or {})
        self.kill_grace = kill_grace

    # Configuration

    def config(self, request: HarnessRequest, model: str) -> dict[str, Any]:
        """The per-attempt OpenCode configuration. It holds no secret: the provider key
        and the endpoint token are `{env:...}` references."""
        deny_all = {"*": "deny", f"{SERVER}_*": "allow"}
        price = self.prices[model]
        return {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "model": f"{PROVIDER}/{model}",
            "small_model": f"{PROVIDER}/{model}",
            "enabled_providers": [PROVIDER],
            "provider": {
                PROVIDER: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": PROVIDER,
                    "options": {
                        "baseURL": self.base_url,
                        "apiKey": "{env:" + KEY_VARIABLE + "}",
                        "headers": self.headers,
                    },
                    "models": {
                        model: {
                            "name": model,
                            "cost": {"input": price.input, "output": price.output},
                        }
                    },
                }
            },
            "mcp": {
                SERVER: {
                    "type": "remote",
                    "url": request.mcp_url,
                    "headers": {"Authorization": "Bearer {env:" + TOKEN_VARIABLE + "}"},
                    "oauth": False,
                    "enabled": True,
                }
            },
            "permission": deny_all,
            "agent": {
                AGENT: {
                    "mode": "primary",
                    "prompt": request.system_prompt,
                    "permission": deny_all,
                }
            },
        }

    def environment(self, request: HarnessRequest, model: str, home: Path) -> dict[str, str]:
        env = {name: os.environ[name] for name in INHERITED if name in os.environ}
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_STATE_HOME": str(home / "state"),
                "TMPDIR": str(home / "tmp"),
                "OPENCODE_DB": str(home / "opencode.db"),
                "OPENCODE_PURE": "1",
                "OPENCODE_CONFIG_CONTENT": json.dumps(self.config(request, model)),
                KEY_VARIABLE: self.api_key or "",
                TOKEN_VARIABLE: request.mcp_token,
                **{f"OPENCODE_DISABLE_{name}": "1" for name in DISABLED},
            }
        )
        return env

    def command(self, model: str) -> list[str]:
        return [
            str(self.binary),
            "run",
            "--format",
            "json",
            "--pure",
            "--title",
            "cardea",
            "--agent",
            AGENT,
            "--model",
            f"{PROVIDER}/{model}",
            "--print-logs",
            "--log-level",
            "WARN",
        ]

    # Running

    async def preflight(self, request: HarnessRequest) -> bool:
        """The endpoint answers an authenticated MCP initialize (no tool listing)."""
        body = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cardea-preflight", "version": "1"},
            },
        }
        headers = {
            "Authorization": f"Bearer {request.mcp_token}",
            "Accept": "application/json, text/event-stream",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(request.mcp_url, json=body, headers=headers)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def run(self, request: HarnessRequest) -> HarnessResult:
        model = request.model or self.default_model
        if model not in self.prices:
            return HarnessResult.failed("model_not_allowed")
        if not await self.preflight(request):
            return HarnessResult.failed(TOOLS_UNAVAILABLE)
        meter = Meter(self.prices[model])
        with tempfile.TemporaryDirectory(prefix="cardea-opencode-") as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            home, work = root / "home", root / "work"
            for path in (home, home / "tmp", work):
                path.mkdir(mode=0o700)
            process = await asyncio.create_subprocess_exec(
                *self.command(model),
                cwd=work,
                env=self.environment(request, model, home),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_LINE,
            )
            try:
                return await self._supervise(process, request, meter)
            finally:
                await self._kill(process)

    async def _supervise(
        self, process: asyncio.subprocess.Process, request: HarnessRequest, meter: Meter
    ) -> HarnessResult:
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(request.prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        watch = asyncio.ensure_future(self._watch_logs(process.stderr))
        try:
            reading = asyncio.ensure_future(self._read(process.stdout, request, meter))
            done, _ = await asyncio.wait({reading, watch}, return_when=asyncio.FIRST_COMPLETED)
            if watch in done and watch.exception() is not None:
                reading.cancel()
                raise watch.exception()  # type: ignore[misc]
            await reading
            code = await process.wait()
        except Stop as stop:
            return HarnessResult(
                text="",
                success=False,
                failure_reason=stop.reason,
                stop_reason=stop.reason,
                turns=meter.steps,
                cost_usd=meter.cost,
                usage=meter.usage(),
            )
        finally:
            watch.cancel()
        text = "".join(meter.texts).strip()
        failure = None
        if code != 0 or meter.errors:
            failure = "harness_error"
        elif meter.last_reason != "stop":
            failure = f"harness_stopped_{meter.last_reason or 'early'}"[:64]
        return HarnessResult(
            text=text,
            success=failure is None,
            failure_reason=failure,
            stop_reason=meter.last_reason,
            turns=meter.steps,
            cost_usd=meter.cost,
            usage=meter.usage(),
        )

    async def _read(
        self, stream: asyncio.StreamReader, request: HarnessRequest, meter: Meter
    ) -> None:
        async for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                meter.record(event)
            except (KeyError, TypeError, ValueError):
                raise Stop("harness_output_invalid") from None
            if event.get("type") == "tool_use" and not meter.tools[-1].startswith(f"{SERVER}_"):
                raise Stop("harness_tool_violation")
            if event.get("type") == "step_finish":
                if meter.cost > request.max_cost_usd:
                    raise Stop("max_cost")
                if meter.steps >= request.max_turns and meter.last_reason != "stop":
                    raise Stop("max_turns")

    @staticmethod
    async def _watch_logs(stream: asyncio.StreamReader) -> None:
        """Stop the run when OpenCode reports Cardea's MCP server failed to connect."""
        async for raw in stream:
            line = raw.decode(errors="replace")
            if f"key={SERVER}" in line and "status=failed" in line:
                raise Stop(TOOLS_UNAVAILABLE)

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), self.kill_grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
