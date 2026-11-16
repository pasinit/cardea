"""The Claude Code harness for `code.session` (ADR-037, lead decision 2026-11-16).

Claude Code runs headless (`-p`) in the scratch checkout, inside the `coding` confined
worker. The binary is the one ADR-033 already pins: the CLI bundled with the pinned
`claude-agent-sdk` (2.1.286 at 0.2.163), unless the host owner names another.

Isolation from the repository's own configuration, which is untrusted:

- `--bare` skips hooks, skills, plugins, MCP servers, auto memory and CLAUDE.md. Claude
  Code documents that bare mode reads neither `CLAUDE_CODE_OAUTH_TOKEN` nor OAuth logins,
  so it is used with an API key (`credential_kind: api_key`). With a subscription token
  (`oauth_token`) the same isolation comes from explicit flags only:
- `--setting-sources user` with a scratch, empty `CLAUDE_CONFIG_DIR` and `HOME`: no
  project or local settings, so no repository hooks or permission rules;
- `--settings {"disableAllHooks": true}`: no hooks from any source;
- `--strict-mcp-config --mcp-config {"mcpServers": {}}`: no MCP server from `.mcp.json`;
- `--disable-slash-commands`: no repository skills or commands;
- `--tools` and `--allowedTools`: editing and running commands, with file tools scoped to
  the checkout; `--permission-mode dontAsk` and `--permission-prompts none` deny the rest;
- `--no-session-persistence`, `--max-turns`, `--max-budget-usd`.

Both modes pass all of these flags; bare mode adds `--bare`. The confined worker, not the
tool list, is what limits commands to the scratch workspace and the network to the model
API.

The model credential is the profile's single credential (ADR-028, ADR-033): systemd loads
it, the worker sees it at `$CARDEA_CREDENTIAL`, and only the harness's environment gets it,
as `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. The proxy variables the helper sets
are passed on so the model API is reachable through the profile's egress.
"""

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from cardea.service.operations import OperationRejected

TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
# File tools are limited to the checkout (the working directory).
ALLOWED = ("Bash", "Read(./**)", "Edit(./**)", "Write(./**)", "Glob", "Grep")
PROXY = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy")
CREDENTIAL_VARIABLE = {"oauth_token": "CLAUDE_CODE_OAUTH_TOKEN", "api_key": "ANTHROPIC_API_KEY"}
MAX_SUMMARY = 2048
DEFAULT_MAX_TURNS = 60
DEFAULT_MAX_BUDGET_USD = 5.0


def bundled_binary() -> Path:
    """The Claude Code CLI bundled with the pinned Agent SDK (ADR-033)."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        raise OperationRejected("the pinned Claude Agent SDK is not installed in the worker")
    return Path(spec.origin).parent / "_bundled" / "claude"


def argv(settings: dict[str, Any]) -> list[str]:
    kind = str(settings.get("credential_kind", "oauth_token"))
    if kind not in CREDENTIAL_VARIABLE:
        raise OperationRejected(f"credential_kind {kind} is not supported")
    binary = str(settings.get("binary") or bundled_binary())
    command = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--model",
        str(settings["model"]),
        "--max-turns",
        str(int(settings.get("max_turns", DEFAULT_MAX_TURNS))),
        "--max-budget-usd",
        str(float(settings.get("max_budget_usd", DEFAULT_MAX_BUDGET_USD))),
        "--tools",
        ",".join(TOOLS),
        "--allowedTools",
        ",".join(ALLOWED),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "user",
        "--settings",
        json.dumps({"disableAllHooks": True}),
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": {}}),
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if kind == "api_key":
        # Bare mode reads an API key but not a subscription token (Claude Code docs).
        command.insert(2, "--bare")
    return command


def environment(base: dict[str, str], scratch: Path, settings: dict[str, Any]) -> dict[str, str]:
    """The harness's environment: the session's base, an empty configuration directory,
    the profile's egress proxy and the model credential, and nothing else."""
    kind = str(settings.get("credential_kind", "oauth_token"))
    path = os.environ.get("CARDEA_CREDENTIAL")
    if not path:
        raise OperationRejected("the coding profile has no model credential")
    secret = Path(path).read_text().strip()
    config = scratch / "claude-config"
    config.mkdir(exist_ok=True)
    env = {**base, "CLAUDE_CONFIG_DIR": str(config)}
    for name in PROXY:
        if name in os.environ:
            env[name] = os.environ[name]
    env[CREDENTIAL_VARIABLE[kind]] = secret
    return env


def result(stdout: str) -> dict[str, JsonValue]:
    """Turns, stop reason, cost and a bounded summary from `--output-format json`."""
    try:
        document = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    cost = document.get("total_cost_usd")
    turns = document.get("num_turns")
    return {
        "turns": turns if isinstance(turns, int) else None,
        "stop_reason": str(document.get("stop_reason") or document.get("subtype") or "") or None,
        "cost_usd": float(cost) if isinstance(cost, int | float) else None,
        "is_error": bool(document.get("is_error", not document)),
        "summary": str(document.get("result") or "")[-MAX_SUMMARY:],
    }
