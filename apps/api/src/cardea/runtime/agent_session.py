"""The Claude Agent SDK harness: one bounded session whose only tools are Cardea's.

ADR-027 introduced it as the first session adapter; ADR-033 makes it one harness of many.

The kernel treats a session as an opaque unit. Every tool it can use is served by the
per-attempt Cardea MCP endpoint; the adapter's job is to make sure nothing else is.
"""

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cardea.runtime import activity as activities
from cardea.runtime import workspace
from cardea.runtime.activity import AttemptActivity
from cardea.runtime.harness import (
    PROXY_VARIABLES,
    Harness,
    HarnessRequest,
    HarnessResult,
    tool_timeout_ms,
)

# Inherited variables a session may keep; all others are blanked (secrets included). Inside
# a confined worker the proxy variables are the only route to the model API.
INHERITED = frozenset(
    {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", *PROXY_VARIABLES}
)
# A workspace's built-in tools; file tools only under the working directory (as in the
# `code.session` harness, ADR-037).
WORKSPACE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
WORKSPACE_ALLOWED = ("Bash", "Read(./**)", "Edit(./**)", "Write(./**)", "Glob", "Grep")
# Name of the MCP server entry; Claude Code exposes its tools as mcp__cardea__<tool>.
SERVER_NAME = "cardea"


def direct_endpoint(mcp_url: str) -> dict[str, str]:
    """NO_PROXY values that keep the tool endpoint off the proxy, when a proxy is set.

    The worker's profile already lists the endpoint address in NO_PROXY; this only adds the
    endpoint's host if a proxy is configured without it, and adds nothing otherwise.
    """
    if not any(os.environ.get(name) for name in PROXY_VARIABLES[:4]):
        return {}
    host = urlsplit(mcp_url).hostname or ""
    values = {}
    for name in ("NO_PROXY", "no_proxy"):
        listed = [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]
        if host and host not in listed:
            values[name] = ",".join([*listed, host])
    return values


# The first harness predates the harness contract; its names remain as aliases.
SessionRequest = HarnessRequest
AgentSession = Harness


class SessionResult(HarnessResult):
    """A `HarnessResult` built from the first adapter's fields (`is_error`, `stop_reason`)."""

    def __init__(
        self,
        *,
        text: str,
        is_error: bool,
        cost_usd: float | None = None,
        turns: int = 0,
        stop_reason: str = "",
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            text=text,
            success=not is_error,
            failure_reason=(stop_reason or "error") if is_error else None,
            cost_usd=cost_usd,
            turns=turns,
            stop_reason=stop_reason,
            usage=usage or {},
        )


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

    def options(self, request: HarnessRequest, workdir: Path) -> dict[str, Any]:
        server: dict[str, Any] = {
            "type": "http",
            "url": request.mcp_url,
            "headers": {"Authorization": f"Bearer {request.mcp_token}"},
        }
        # Claude Code bounds each MCP tool call three ways: a per-call timeout, the HTTP
        # request timeout (60 s unless raised) and an idle timeout (5 min for HTTP servers).
        # The per-server `timeout` (ms) raises all three for this server; MCP_TOOL_TIMEOUT
        # is the documented global equivalent and gets the same value.
        timeout = tool_timeout_ms(request)
        if timeout is not None:
            server["timeout"] = timeout
        mcp_tools = [f"mcp__{SERVER_NAME}__{name}" for name in request.tool_names]
        space = request.workspace
        return {
            "system_prompt": request.system_prompt
            + (workspace.instructions(space.repositories, workdir) if space else ""),
            # No built-in tools at all: shell, files, web, scheduling and subagents are off,
            # unless the run has a workspace (ADR-037 amendment 2026-11-24): then editing
            # and a shell, with the file tools scoped to the working directory.
            "tools": list(WORKSPACE_TOOLS) if space else [],
            "allowed_tools": [*WORKSPACE_ALLOWED, *mcp_tools] if space else mcp_tools,
            "mcp_servers": {SERVER_NAME: server},
            # Ignore any MCP servers, settings, skills or plugins found on the host.
            "strict_mcp_config": True,
            "setting_sources": [],
            "permission_mode": "bypassPermissions",
            "max_turns": request.max_turns,
            "max_budget_usd": request.max_cost_usd,
            "model": request.model or self.model,
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
                **({"MCP_TOOL_TIMEOUT": str(timeout)} if timeout is not None else {}),
                # Explicit rather than left to the SDK's merge of the parent environment.
                **{name: os.environ[name] for name in PROXY_VARIABLES if name in os.environ},
                **direct_endpoint(request.mcp_url),
                **(
                    workspace.environment(
                        workdir, space.upload_url, request.mcp_token, space.memory_url
                    )
                    if space
                    else {}
                ),
            },
        }

    async def run(self, request: HarnessRequest) -> HarnessResult:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        activity = activities.current()
        activity.redact(request.mcp_token, self.api_key, self.oauth_token)
        began = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="cardea-session-") as directory:
            os.chmod(directory, 0o700)
            if request.workspace is not None:
                try:
                    workspace.prepare(
                        Path(directory), request.workspace.repositories, request.memory
                    )
                except workspace.WorkspaceRefused as refused:
                    activity.event(f"workspace refused: {refused}", logging.WARNING)
                    return HarnessResult.failed("workspace_unavailable")
                activity.event("workspace ready", repositories=len(request.workspace.repositories))
            options = ClaudeAgentOptions(
                **self.options(request, Path(directory)),
                # The CLI's stderr, line by line, bounded and scrubbed (never a debug dump).
                stderr=activity.harness_output,
            )
            result: ResultMessage | None = None
            activity.event("sdk session starting")
            try:
                async for message in query(prompt=request.prompt, options=options):
                    observe(activity, message)
                    if isinstance(message, ResultMessage):
                        result = message
            except Exception as error:
                activity.event(
                    "sdk session failed",
                    logging.WARNING,
                    error=type(error).__name__,
                    returncode=getattr(error, "exit_code", None),
                    duration_s=round(time.monotonic() - began, 3),
                )
                raise
        activity.event("sdk session ended", duration_s=round(time.monotonic() - began, 3))
        if result is None:
            return HarnessResult.failed("no_result")
        stop = str(result.stop_reason or result.subtype)
        return HarnessResult(
            text=result.result or "",
            success=not result.is_error,
            failure_reason=(stop or "error") if result.is_error else None,
            cost_usd=result.total_cost_usd,
            turns=result.num_turns,
            stop_reason=stop,
            usage=dict(result.usage or {}),
        )


def observe(activity: AttemptActivity, message: object) -> None:
    """Log one SDK message as metadata: kinds, counts, tool names, codes; never content."""
    from claude_agent_sdk import (
        AssistantMessage,
        RateLimitEvent,
        ResultMessage,
        SystemMessage,
        ToolUseBlock,
        UserMessage,
    )

    if isinstance(message, AssistantMessage):
        usage = message.usage or {}
        activity.model_activity(
            "turn",
            logging.WARNING if message.error else logging.INFO,
            blocks=",".join(type(block).__name__ for block in message.content),
            tools=[b.name for b in message.content if isinstance(b, ToolUseBlock)] or None,
            stop_reason=message.stop_reason,
            error=message.error,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
    elif isinstance(message, UserMessage):
        # Tool results went back to the model; the next model call is now in flight.
        activity.model_activity("request", logging.DEBUG)
    elif isinstance(message, ResultMessage):
        activity.model_activity(
            "result",
            logging.WARNING if message.is_error else logging.INFO,
            outcome=message.subtype,
            stop_reason=message.stop_reason,
            turns=message.num_turns,
            cost_usd=message.total_cost_usd,
            code=message.api_error_status,
            reason=message.terminal_reason,
        )
    elif isinstance(message, SystemMessage):
        servers = message.data.get("mcp_servers") if isinstance(message.data, dict) else None
        mcp = (
            [f"{s.get('name')}:{s.get('status')}" for s in servers if isinstance(s, dict)]
            if isinstance(servers, list)
            else None
        )
        activity.event(f"sdk system message: {message.subtype}", step=message.subtype, mcp=mcp)
    elif isinstance(message, RateLimitEvent):
        info = message.rate_limit_info
        activity.model_activity(
            "rate_limit",
            logging.INFO if info.status == "allowed" else logging.WARNING,
            outcome=info.status,
            reason=info.rate_limit_type,
        )
    else:
        activity.event(f"sdk message: {type(message).__name__}", logging.DEBUG)
