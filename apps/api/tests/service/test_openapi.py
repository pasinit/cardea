"""The committed API document matches the app and describes every route's bodies."""

import json
from typing import Any

from cardea.service.schema import DOCUMENT, render, schema


def test_committed_openapi_document_is_current() -> None:
    assert DOCUMENT.read_text() == render(), (
        "packages/kernel-openapi.json is stale: run `make kernel-openapi`"
    )


def operations() -> list[tuple[str, str, dict[str, Any]]]:
    paths: dict[str, dict[str, dict[str, Any]]] = schema()["paths"]  # type: ignore[assignment]
    return [(method, path, op) for path, ops in paths.items() for method, op in ops.items()]


def typed(body: dict[str, Any]) -> bool:
    found = body.get("content", {}).get("application/json", {}).get("schema", {})
    return "$ref" in found or (found.get("type") == "array" and "$ref" in found.get("items", {}))


def test_every_route_documents_its_request_and_response_bodies() -> None:
    listed = operations()
    assert len(listed) >= 20
    for method, path, op in listed:
        success = [code for code in op["responses"] if code.startswith("2")]
        assert len(success) == 1, (method, path)
        assert typed(op["responses"][success[0]]), f"{method} {path} has no response schema"
        assert typed(op["responses"]["401"]), f"{method} {path} has no error schema"
        if "requestBody" in op:
            assert typed(op["requestBody"]), f"{method} {path} has no request schema"
    # Submissions, triggers and control signals take a body.
    with_body = {f"{m} {p}" for m, p, op in listed if "requestBody" in op}
    assert {"post /v0.2/agent-runs", "put /v0.2/triggers/{trigger_id}"} <= with_body
    assert "post /v0.2/agent-tasks" not in {f"{m} {p}" for m, p, _ in listed}


def test_document_is_valid_json_with_a_version() -> None:
    document = json.loads(DOCUMENT.read_text())
    assert document["openapi"].startswith("3.") and document["info"]["version"] == "0.2"
