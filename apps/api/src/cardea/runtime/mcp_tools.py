"""Native MCP boundary. Only trusted installed code constructs the client/allowlist."""

import asyncio
from typing import Any

import rfc8785
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.types import CallToolResult, Tool


class ToolFailure(Exception):
    pass


class MCPTools:
    def __init__(self, client: Client, allowed: dict[str, dict[str, Any]]) -> None:
        self.client, self.allowed = client, allowed

    async def discover(self) -> list[Tool]:
        result = await self.client.list_tools()
        if result.next_cursor is not None or len(result.tools) > 32:
            raise ToolFailure("unsupported_tool_catalog")
        permitted: list[Tool] = []
        for tool in result.tools:
            if tool.name not in self.allowed:
                continue
            if rfc8785.dumps(tool.input_schema) != rfc8785.dumps(self.allowed[tool.name]):
                raise ToolFailure("installed_tool_schema_changed")
            if len(rfc8785.dumps(tool.model_dump(mode="json"))) > 16384:
                raise ToolFailure("tool_descriptor_too_large")
            if any(previous.name == tool.name for previous in permitted):
                raise ToolFailure("duplicate_tool")
            Draft202012Validator.check_schema(tool.input_schema)
            permitted.append(tool)
        if {tool.name for tool in permitted} != set(self.allowed):
            raise ToolFailure("installed_tool_missing")
        return permitted

    async def call(self, tool: Tool, arguments: dict[str, Any]) -> CallToolResult:
        if tool.name not in self.allowed or tool.input_schema != self.allowed[tool.name]:
            raise ToolFailure("tool_denied")
        if len(rfc8785.dumps(arguments)) > 16384:
            raise ToolFailure("tool_input_too_large")
        Draft202012Validator(tool.input_schema).validate(arguments)
        try:
            async with asyncio.timeout(15):
                result = await self.client.call_tool(tool.name, arguments, read_timeout_seconds=12)
        except Exception:
            raise ToolFailure("tool_outcome_unknown") from None
        if result.is_error or result.structured_content is None:
            raise ToolFailure("tool_failed")
        if len(rfc8785.dumps(result.model_dump(mode="json"))) > 65536:
            raise ToolFailure("tool_output_too_large")
        # Never follow result links, call embedded tools or grant annotation-derived authority.
        return result
