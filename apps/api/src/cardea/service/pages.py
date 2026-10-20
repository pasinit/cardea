"""Response bodies that always fit the response cap (ADR-029, bounded run evidence).

The API refuses any response over `MAX_RESPONSE_BYTES`. A run's calls and receipts can
together be far larger (hundreds of calls, each with up to 64 KiB of arguments and result), so:
- the run view summarizes each call (sizes, digests and bounded previews) and pages its calls;
- receipt listings page by size. One receipt always fits: its arguments and its result are
  each a kernel document of at most 64 KiB (admission and settlement refuse more);
- a task's output document (up to 1 MiB) is served whole when it fits, and as base64 chunks
  of its stored bytes otherwise.
"""

import base64
import json
from collections.abc import Callable, Iterable
from hashlib import sha256
from typing import Any

import rfc8785
from fastapi.encoders import jsonable_encoder
from pydantic import JsonValue

from cardea.kernel.models import OperationReceipt
from cardea.service.database import Fault

MAX_RESPONSE_BYTES = 262144
# Room left for the envelope of a paged body and JSON encoding differences.
PAGE_BUDGET = MAX_RESPONSE_BYTES - 16384
# The run view's own output and verification documents share this budget.
DOCUMENT_BUDGET = 65536
# Raw bytes per chunk: base64 makes 174,763 characters, well under the cap.
CHUNK_BYTES = 131072
DEFAULT_PREVIEW_BYTES = 2048
MAX_PREVIEW_BYTES = 16384


def encoded_size(value: object) -> int:
    """Bytes of `value` as the API serializes it (compact UTF-8 JSON)."""
    data = json.dumps(jsonable_encoder(value), separators=(",", ":"), ensure_ascii=False)
    return len(data.encode())


def canonical(value: JsonValue) -> bytes:
    return rfc8785.dumps(value)


def preview(data: bytes, limit: int) -> str:
    """At most `limit` bytes of UTF-8 text, cut on a character boundary."""
    return data[:limit].decode("utf-8", "ignore")


def page[T](
    items: Iterable[T],
    key: Callable[[T], str],
    render: Callable[[T], object],
    envelope: object,
    limit: int,
) -> tuple[list[object], str | None]:
    """Render items until `limit` or the size budget is reached; return them and a cursor.

    The first item is always included, so a page makes progress; callers keep single items
    below the budget. The cursor is the key of the last item when more remain.
    """
    rendered: list[object] = []
    used = encoded_size(envelope)
    last: str | None = None
    remaining = False
    for item in items:
        if len(rendered) >= limit:
            remaining = True
            break
        body = render(item)
        size = encoded_size(body) + 1
        if rendered and used + size > PAGE_BUDGET:
            remaining = True
            break
        rendered.append(body)
        used += size
        last = key(item)
    return rendered, last if remaining else None


def after[T](items: list[T], key: Callable[[T], str], cursor: str) -> list[T]:
    """The items following `cursor` (empty cursor: all of them)."""
    if not cursor:
        return items
    for index, item in enumerate(items):
        if key(item) == cursor:
            return items[index + 1 :]
    raise Fault("invalid_cursor", "Cursor does not name an item of this list", 400)


def call_summary(receipt: OperationReceipt, preview_bytes: int) -> dict[str, Any]:
    """One call of the run view: what happened, with sizes, digests and bounded previews.

    `arguments` and `result` are inline only when they fit the preview size; fetch a larger
    result from `GET /v0.2/tasks/{id}/operations/{operation_id}/result`.
    """
    arguments = receipt.request.arguments.get("arguments")
    argument_bytes = canonical(arguments)
    result = receipt.result
    output = canonical(result.get("output")) if result is not None else None
    small = output is not None and len(output) <= preview_bytes
    return {
        "operation_id": receipt.request.operation_id,
        "operation": receipt.request.name,
        "state": receipt.state.value,
        "receipt_id": receipt.receipt_id,
        "created_at": receipt.created_at.isoformat(),
        "updated_at": receipt.updated_at.isoformat(),
        "failure": receipt.failure.code if receipt.failure else None,
        "failure_details": receipt.failure.details if receipt.failure else None,
        "reconciled": bool(receipt.reconciliation_evidence),
        "deduplicated": bool(result and result.get("deduplicated")),
        "arguments": arguments if len(argument_bytes) <= preview_bytes else None,
        "arguments_bytes": len(argument_bytes),
        "arguments_sha256": sha256(argument_bytes).hexdigest(),
        "arguments_preview": preview(argument_bytes, preview_bytes),
        "result": result if small else None,
        "result_bytes": len(output) if output is not None else None,
        "result_sha256": sha256(output).hexdigest() if output is not None else None,
        "result_preview": preview(output, preview_bytes) if output is not None else None,
    }


def bounded_document(document: object) -> tuple[object, bool]:
    """A run-view document within its budget, and whether it was cut.

    An agent run's output keeps its text, trimmed if it must be, and drops its `calls` list
    (the run view lists the calls). Any other over-large document is omitted. The whole
    document stays available from `GET /v0.2/tasks/{id}/output`.
    """
    if document is None or encoded_size(document) <= DOCUMENT_BUDGET:
        return document, False
    if not isinstance(document, dict) or not isinstance(document.get("text"), str):
        return None, True
    trimmed: dict[str, Any] = {k: v for k, v in document.items() if k != "calls"}
    text = trimmed["text"].encode()
    room = DOCUMENT_BUDGET - encoded_size({**trimmed, "text": ""})
    while encoded_size({**trimmed, "text": preview(text, room)}) > DOCUMENT_BUDGET and room > 0:
        room -= max(1, room // 8)  # escaping can make the JSON longer than the raw text
    trimmed["text"] = preview(text, max(0, room))
    return trimmed, True


def chunk(data: bytes, offset: int) -> dict[str, Any]:
    """One base64 chunk of `data` starting at `offset`."""
    if offset > len(data):
        raise Fault("invalid_offset", "Offset is past the end", 400)
    end = min(len(data), offset + CHUNK_BYTES)
    return {
        "schema_version": "0.2",
        "total_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "offset": offset,
        "next_offset": end if end < len(data) else None,
        "data_base64": base64.b64encode(data[offset:end]).decode(),
    }
