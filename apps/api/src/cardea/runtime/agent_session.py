"""Agent session adapters: one bounded session whose only tools are Cardea's (ADR-027).

The kernel treats a session as an opaque unit. Every tool it can use is served by the
per-attempt Cardea MCP endpoint; the adapter's job is to make sure nothing else is.
"""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Inherited variables a session may keep; all others are blanked (secrets included).
INHERITED = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"})
# Name of the MCP server entry; Claude Code exposes its tools as mcp__cardea__<tool>.
SERVER_NAME = "cardea"


@dataclass(frozen=True)
class SessionRequest:
    system_prompt: str
    prompt: str
    mcp_url: str
    mcp_token: str
    tool_names: tuple[str, ...]
    max_turns: int
    max_cost_usd: float
    timeout_seconds: float
    # Stable identity of this attempt (task and attempt number); a confined session's
    # worker job derives from it so recovery can stop it (ADR-028).
    job_key: str = ""


@dataclass(frozen=True)
class SessionResult:
    text: str
    is_error: bool
    cost_usd: float | None = None
    turns: int = 0
    stop_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class AgentSession(Protocol):
    async def run(self, request: SessionRequest) -> SessionResult: ...


class ClaudeAgentSession:
    """Claude Agent SDK session with every built-in tool and setting source disabled."""

    def __init__(
        self,
        *,
        model: str | None,
        api_key: str | None = None,
        oauth_token: str | None = None,
    ) -> None:
        if (api_key is None) == (oauth_token is None):
            raise ValueError("Select exactly one credential")
        self.model = model
        self.api_key = api_key
        self.oauth_token = oauth_token

    def options(self, request: SessionRequest, workdir: Path) -> dict[str, Any]:
        return {
            "system_prompt": request.system_prompt,
            # No built-in tools at all: shell, files, web, scheduling and subagents are off.
            "tools": [],
            "allowed_tools": [f"mcp__{SERVER_NAME}__{name}" for name in request.tool_names],
            "mcp_servers": {
                SERVER_NAME: {
                    "type": "http",
                    "url": request.mcp_url,
                    "headers": {"Authorization": f"Bearer {request.mcp_token}"},
                }
            },
            # Ignore any MCP servers, settings, skills or plugins found on the host.
            "strict_mcp_config": True,
            "setting_sources": [],
            "permission_mode": "bypassPermissions",
            "max_turns": request.max_turns,
            "max_budget_usd": request.max_cost_usd,
            "model": self.model,
            "cwd": str(workdir),
            # The SDK starts the CLI with the service's whole environment and layers this
            # mapping on top, so every inherited variable outside a small allow-list is
            # blanked: the CLI gets its credential, a private home and nothing else.
            "env": {
                **{name: "" for name in os.environ if name not in INHERITED},
                "HOME": str(workdir),
                "ANTHROPIC_API_KEY": self.api_key or "",
                "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token or "",
                "ENABLE_TOOL_SEARCH": "false",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        }

    async def run(self, request: SessionRequest) -> SessionResult:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        with tempfile.TemporaryDirectory(prefix="cardea-session-") as directory:
            os.chmod(directory, 0o700)
            options = ClaudeAgentOptions(**self.options(request, Path(directory)))
            result: ResultMessage | None = None
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        if result is None:
            return SessionResult(text="", is_error=True, stop_reason="no_result")
        return SessionResult(
            text=result.result or "",
            is_error=result.is_error,
            cost_usd=result.total_cost_usd,
            turns=result.num_turns,
            stop_reason=str(result.stop_reason or result.subtype),
            usage=dict(result.usage or {}),
        )
