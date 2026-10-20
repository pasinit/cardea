"""The incident example as an installed read operation (ADR-023), run by an agent (ADR-027).

Until ADR-032 this lived in the kernel as a fixed MCP tool behind its own agent-task API.
It is now ordinary application code: a descriptor plus a handler, installed like any other.
"""

import json
from pathlib import Path

from cardea.service.operations import OperationContext, OperationDescriptor
from incidents import IncidentArtifacts, IncidentBatch
from pydantic import JsonValue

DESCRIPTOR = Path(__file__).with_name("incidents.compile.json")


def descriptor() -> OperationDescriptor:
    return OperationDescriptor.model_validate_json(DESCRIPTOR.read_bytes())


def build(batch: IncidentBatch) -> IncidentArtifacts:
    """A sorted index and a Markdown summary that names every record exactly once."""
    records = sorted(batch.records, key=lambda record: record.id)
    summary = (
        "# Incident summary\n\n"
        + "\n".join(
            f"- [{r.id}] {json.dumps(r.title, ensure_ascii=True)} ({r.severity})" for r in records
        )
        + "\n"
    )
    return IncidentArtifacts(index=records, summary=summary)


async def compile_incidents(
    arguments: dict[str, JsonValue], context: OperationContext
) -> dict[str, JsonValue]:
    batch = IncidentBatch.model_validate(arguments["batch"])
    result: dict[str, JsonValue] = build(batch).model_dump(mode="json")
    return result
