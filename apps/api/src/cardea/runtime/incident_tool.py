"""Fixed MCP application server; the kernel API is not exposed through MCP."""

import json

from mcp.server import MCPServer

from cardea.runtime.incidents import IncidentArtifacts, IncidentBatch

server = MCPServer("cardea-incidents", version="1", log_level="ERROR")


@server.tool()
def compile_incidents(batch: IncidentBatch) -> IncidentArtifacts:
    """Build a sorted index and a Markdown summary of the supplied incident records."""
    records = sorted(batch.records, key=lambda record: record.id)
    # This portable reference tool is for SDK fit. Debian integration must use a confined worker.
    summary = (
        "# Incident summary\n\n"
        + "\n".join(
            f"- [{record.id}] {json.dumps(record.title, ensure_ascii=True)} ({record.severity})"
            for record in records
        )
        + "\n"
    )
    return IncidentArtifacts(index=records, summary=summary)


if __name__ == "__main__":
    server.run(transport="stdio")
