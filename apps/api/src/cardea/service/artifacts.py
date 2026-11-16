"""Artifacts of confined operations: large results stored as kernel objects (ADR-037).

A receipt records at most a 64 KiB document. A confined operation that declares
`max_artifact_bytes` may return named artifacts beside its output, for example a coding
session's change as a git bundle. The kernel stores each artifact as task-owned objects in
parts of at most 1 MiB (the store's object limit), and the output in a further object, all
in one transaction, before the receipt settles. The output the receipt records then carries
the artifacts' object references instead of their bytes.

Because the output is stored with the artifacts, an interrupted call is reconciled from the
store: the stored output means the call completed; its absence means nothing was published,
and a retry runs the operation again (it had no other effect).
"""

import base64
import binascii
import json
import re
from hashlib import sha256
from typing import Any

import rfc8785
from pydantic import JsonValue

from cardea.kernel.models import ObjectVersion, ResourceRef
from cardea.kernel.store import MAX_OBJECT_BYTES, StoreError
from cardea.service.database import Database, borrow
from cardea.service.models import now
from cardea.service.operations import OperationContext, Reconciler, Reconciliation

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}(\.[a-z0-9]{1,8})?$")
MAX_ARTIFACTS = 8
OUTPUT = "output"


class ArtifactRefused(Exception):
    """The worker returned artifacts the operation may not store; nothing was stored."""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


def decode(raw: object, limit: int) -> dict[str, bytes]:
    """Artifacts as a worker returned them: `{name: base64}`, within the operation's limit."""
    if not isinstance(raw, dict) or not raw:
        raise ArtifactRefused("artifacts must be a non-empty object")
    if len(raw) > MAX_ARTIFACTS:
        raise ArtifactRefused(f"at most {MAX_ARTIFACTS} artifacts")
    decoded: dict[str, bytes] = {}
    total = 0
    for name, value in raw.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == OUTPUT:
            raise ArtifactRefused("an artifact name is not allowed")
        try:
            content = base64.b64decode(str(value), validate=True)
        except (ValueError, binascii.Error):
            raise ArtifactRefused(f"artifact {name} is not base64") from None
        total += len(content)
        if total > limit:
            raise ArtifactRefused(f"artifacts exceed the operation's {limit} byte limit")
        decoded[name] = content
    return decoded


def _reference(context: OperationContext, name: str, part: int | None = None) -> ResourceRef:
    suffix = name if part is None else f"{name}.{part:03d}"
    return ResourceRef(namespace=context.namespace, resource_id=f"{context.receipt_id}.{suffix}")


def store(
    database: Database,
    context: OperationContext,
    output: dict[str, JsonValue],
    artifacts: dict[str, bytes],
    producer_id: str,
) -> dict[str, JsonValue]:
    """Store artifacts and the output in one transaction, fenced by the call's lease."""
    listed: list[JsonValue] = []
    at = now()
    with database.engine.begin() as conn, borrow(conn) as tx:
        state = conn.exec_driver_sql(
            "SELECT state FROM operations WHERE receipt_id=?", (context.receipt_id,)
        ).scalar()
        if state != "running":
            raise ArtifactRefused("the call is no longer running")
        for name, content in sorted(artifacts.items()):
            parts: list[JsonValue] = []
            for index, start in enumerate(range(0, max(1, len(content)), MAX_OBJECT_BYTES)):
                version = tx.put_object(
                    _reference(context, name, index),
                    content[start : start + MAX_OBJECT_BYTES],
                    expected_version=0,
                    producer_id=producer_id,
                    task_id=context.task_id,
                    lease_id=context.lease_id,
                    at=at,
                )
                parts.append(version.model_dump(mode="json"))
            listed.append(
                {
                    "name": name,
                    "bytes": len(content),
                    "sha256": sha256(content).hexdigest(),
                    "parts": parts,
                }
            )
        result: dict[str, JsonValue] = {**output, "artifacts": listed}
        tx.put_object(
            _reference(context, OUTPUT),
            rfc8785.dumps(result),
            expected_version=0,
            producer_id=producer_id,
            task_id=context.task_id,
            lease_id=context.lease_id,
            at=at,
        )
    return result


def stored_output(database: Database, context: OperationContext) -> dict[str, Any] | None:
    """The output an interrupted call stored with its artifacts, if it got that far."""
    with database.read() as conn, borrow(conn) as tx:
        try:
            version = tx.latest_object(_reference(context, OUTPUT))
        except StoreError:
            return None
        document = json.loads(tx.read_object(version))
    return document if isinstance(document, dict) else None


def read(database: Database, artifact: dict[str, Any]) -> bytes:
    """An artifact's bytes from its listed parts, checked against its digest."""
    with database.read() as conn, borrow(conn) as tx:
        content = b"".join(
            tx.read_object(ObjectVersion.model_validate(part)) for part in artifact["parts"]
        )
    if sha256(content).hexdigest() != artifact["sha256"]:
        raise ValueError("artifact digest mismatch")
    return content


def reconciler(database: Database) -> Reconciler:
    """The kernel's reconciler for confined operations with artifacts (ADR-026 item 4)."""

    async def reconcile(
        arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        output = stored_output(database, context)
        return Reconciliation(applied=output is not None, output=output)

    return reconcile
