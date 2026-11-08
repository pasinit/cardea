"""Event triggers (ADR-035): signed inbound CloudEvents admitted once per event.

An event arrives as one HTTP request in CloudEvents 1.0 structured JSON mode, signed with
Standard Webhooks (HMAC-SHA256 over `webhook-id.webhook-timestamp.body`) under the
trigger's host-owner secret. Structured mode keeps every event attribute inside the
signed body; binary mode's `ce-*` headers are not covered by the signature and are refused.

A matching event is admitted through `Kernel.admit_scheduled`, the same path and live
ceiling checks as a time-trigger slot, in one SQLite transaction with its delivery row.
The delivery's (trigger, source, id) is unique among admissions, and the request ID
`<trigger_id>.e<digest of source and id>` is a second guard, so a redelivered or replayed
event returns the task it already started, across restarts and retries. A refusal is
recorded and leaves the event open: the sender's retry is evaluated again.
"""

import logging
import time
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import rfc8785
import sqlalchemy as sa
from cloudevents.core.bindings.http import HTTPMessage, from_structured
from cloudevents.core.formats.json import JSONFormat
from cloudevents.core.v1.event import CloudEvent
from pydantic import ValidationError
from standardwebhooks.webhooks import Webhook

from cardea.kernel.lifecycle import ContractError
from cardea.kernel.store import StoreError
from cardea.service.database import Fault, borrow, private_file
from cardea.service.models import InboundEvent, TriggerSpec, now

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)

STRUCTURED = "application/cloudevents+json"
# Attributes the trigger reads itself; every other context attribute is filterable.
DATA_MEMBERS = {"data", "data_base64"}


def load_secrets(path: Path) -> tuple[Webhook, ...]:
    """The trigger's Standard Webhooks secrets, one `whsec_...` per line."""
    try:
        lines = private_file(path).decode("ascii").split()
        secrets = tuple(Webhook(line) for line in lines if line.startswith("whsec_"))
        if not secrets or len(secrets) != len(lines):
            raise ValueError("Unexpected secret format")
    except Exception:
        raise Fault(
            "event_secret_unavailable", "The trigger's signing secret cannot be used", 503
        ) from None
    return secrets


def verify(
    secrets: tuple[Webhook, ...], headers: Mapping[str, str], body: bytes, tolerance: int
) -> None:
    """Standard Webhooks verification, with the host's (at most 5 minute) tolerance."""
    try:
        stamp = int(headers.get("webhook-timestamp", ""))
    except ValueError:
        stamp = None
    if stamp is None or abs(time.time() - stamp) > tolerance:
        raise Fault("invalid_signature", "Event signature could not be verified", 401)
    signed = {
        name: headers.get(name, "")
        for name in ("webhook-id", "webhook-timestamp", "webhook-signature")
    }
    for secret in secrets:
        try:
            secret.verify(body, signed, json_parse=False)
            return
        except Exception:
            continue
    raise Fault("invalid_signature", "Event signature could not be verified", 401)


def _text(value: object) -> str | None:
    """A context attribute's canonical string form (CloudEvents 1.0 type system)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | str):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return None


def parse(headers: Mapping[str, str], body: bytes) -> tuple[InboundEvent, dict[str, str]]:
    """The event and its string-valued context attributes, from structured JSON mode."""
    if any(name.startswith("ce-") for name in headers):
        raise Fault(
            "event_mode_unsupported", "Send the event in CloudEvents structured JSON mode", 415
        )
    media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media != STRUCTURED:
        raise Fault("event_mode_unsupported", f"Content-Type must be {STRUCTURED}", 415)
    try:
        event = from_structured(HTTPMessage(dict(headers), body), JSONFormat(), CloudEvent)
        if event.get_specversion() != "1.0":
            raise ValueError("Only CloudEvents 1.0 is supported")
        data = event.get_data()
        content = event.get_datacontenttype()
        if isinstance(data, bytes) or (
            content is not None
            and content.split(";", 1)[0].strip().lower() != "application/json"
            and not content.split(";", 1)[0].strip().lower().endswith("+json")
        ):
            raise ValueError("Event data must be JSON")
        attributes = {
            name: text
            for name, value in event.get_attributes().items()
            if name not in DATA_MEMBERS and (text := _text(value)) is not None
        }
        inbound = InboundEvent(
            id=event.get_id(),
            source=event.get_source(),
            type=event.get_type(),
            subject=event.get_subject(),
            time=attributes.get("time"),
            data=data,
        )
    except Fault:
        raise
    except Exception:
        raise Fault("invalid_event", "Body is not a supported CloudEvents 1.0 event", 400) from None
    return inbound, attributes


def request_id(trigger_id: str, event: InboundEvent) -> str:
    """`<trigger_id>.e<15 hex>`: at most 128 characters, so still an Identifier."""
    key = sha256(rfc8785.dumps([event.source, event.id])).hexdigest()
    return f"{trigger_id}.e{key[:15]}"


def _trigger(conn: sa.Connection, trigger_id: str) -> sa.RowMapping:
    row = (
        conn.exec_driver_sql(
            "SELECT * FROM triggers WHERE trigger_id=? AND kind='event' AND enabled=1 "
            "AND removed=0",
            (trigger_id,),
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Fault("trigger_not_found", "No active event trigger", 404)
    return row


def _admitted(conn: sa.Connection, trigger_id: str, event: InboundEvent) -> str | None:
    row = conn.exec_driver_sql(
        "SELECT task_id FROM trigger_deliveries WHERE trigger_id=? AND source=? AND event_id=? "
        "AND outcome='admitted'",
        (trigger_id, event.source, event.id),
    ).first()
    return None if row is None else str(row[0])


def _record(
    conn: sa.Connection,
    trigger_id: str,
    event: InboundEvent,
    *,
    task_id: str | None = None,
    code: str | None = None,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO trigger_deliveries(trigger_id,source,event_id,event_type,outcome,task_id,"
        "code,at) VALUES (?,?,?,?,?,?,?,?)",
        (
            trigger_id,
            event.source,
            event.id,
            event.type,
            "admitted" if task_id else "refused",
            task_id,
            code,
            now().isoformat(),
        ),
    )


def deliver(
    kernel: "Kernel", trigger_id: str, headers: Mapping[str, str], body: bytes
) -> dict[str, Any]:
    """Authenticate, match and admit one inbound event; at most one task per event."""
    headers = {name.lower(): value for name, value in headers.items()}
    limits = kernel.event_triggers
    if len(body) > limits.max_event_bytes:
        raise Fault("event_too_large", f"Event exceeds {limits.max_event_bytes} bytes", 413)
    with kernel.database.read() as conn:
        state = _trigger(conn, trigger_id)
    spec = TriggerSpec.model_validate_json(state["definition_json"])
    assert spec.event is not None, "event trigger"
    verify(load_secrets(spec.event.secret_file), headers, body, limits.timestamp_tolerance_seconds)
    event, attributes = parse(headers, body)
    result: dict[str, Any] = {"schema_version": "0.2", "trigger_id": trigger_id}
    if not spec.event.matches(event, attributes):
        return {**result, "outcome": "ignored", "task_id": None}
    expected = state["definition_sha256"]
    try:
        with kernel.database.engine.begin() as conn, borrow(conn) as tx:
            current = _trigger(conn, trigger_id)
            if current["definition_sha256"] != expected:
                raise Fault("trigger_changed", "The trigger changed; deliver the event again", 409)
            existing = _admitted(conn, trigger_id, event)
            if existing is not None:
                return {**result, "outcome": "duplicate", "task_id": existing}
            request = spec.request(request_id(trigger_id, event), event=event)
            view = kernel.admit_scheduled(
                conn,
                tx,
                spec.supervisor_id,
                request,
                {"trigger_id": trigger_id, "event_source": event.source, "event_id": event.id},
            )
            _record(conn, trigger_id, event, task_id=view.task.task_id)
            kernel.fault("event.before_commit")
    except (Fault, ValidationError, StoreError, ContractError) as error:
        if isinstance(error, Fault) and error.code in {"trigger_not_found", "trigger_changed"}:
            raise
        fault = (
            error
            if isinstance(error, Fault)
            else Fault("invalid_template", "The trigger's template is no longer valid", 422)
            if isinstance(error, ValidationError)
            else Fault(str(error.code), "Operation cannot be applied to current state", 409)
        )
        # The admission rolled back; the refusal is history, and the event stays open.
        with kernel.database.engine.begin() as conn:
            _record(conn, trigger_id, event, code=fault.code)
        log.warning("event admission refused", extra={"code": fault.code, "reason": trigger_id})
        raise fault from None
    kernel.fault("event.after_commit")
    return {**result, "outcome": "admitted", "task_id": view.task.task_id}
