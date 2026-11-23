"""Attaching a run's result to a work item, kernel-side (ADR-038 amendment).

An operation whose descriptor names `attach_record` (a pointer to a record ID argument, as
`env.try` does with `record_id`) leaves a link on that record: the full commit, the
scenarios, pass or fail and the receipt ID of the run that produced the result. The agent
never writes it, so it cannot claim a result no run produced.

- **Authority** is the live ceiling on `record:<collection>/attach`, checked when the call
  is admitted (so nothing runs for a role without it) and again when the result is
  recorded. A refusal names the grant.
- **Only the links change.** Holder, lease, claim epoch and state stay as they were; the
  version moves, so a writer holding an older copy is refused rather than erasing the link.
- **One entry per idempotency key.** The link's `external_id` is the call's idempotency
  context, or its receipt ID without one; a second call with the same key adds nothing.
"""

import json
from datetime import datetime
from urllib.parse import quote

import sqlalchemy as sa
from pydantic import JsonValue

from cardea.service.database import Fault
from cardea.service.models import now, record_grant
from cardea.service.operations import OperationRejected
from cardea.service.policy import Policy
from cardea.service.records import MAX_LINKS, append_history, journal, record_view

ACTION = "attach"
REL = "env.try"


def _record(conn: sa.Connection, namespace: str, record_id: str) -> sa.RowMapping:
    row = (
        conn.exec_driver_sql(
            "SELECT * FROM records WHERE namespace=? AND record_id=?", (namespace, record_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise OperationRejected("No such record", "record_not_found")
    return row


def authorize(conn: sa.Connection, task_id: str, namespace: str, record_id: str) -> sa.RowMapping:
    """The record, if the task's live ceiling holds `record:<collection>/attach` for it."""
    row = _record(conn, namespace, record_id)
    grant = record_grant(str(row["collection"]), ACTION)
    try:
        Policy(conn).ceiling(task_id, grant)
    except Fault as denied:
        raise Fault(
            denied.code,
            f"{denied.message}: attaching to {row['collection']}/{record_id} needs {grant}",
            403,
            details=denied.details,
        ) from None
    return row


def link(output: dict[str, JsonValue], receipt_id: str, key: str) -> dict[str, JsonValue]:
    """The link for a result: pass or fail in `observed_state`, the rest in its `url`."""
    listed = output.get("scenarios")
    scenarios = [
        str(item["name"])
        for item in (listed if isinstance(listed, list) else [])
        if isinstance(item, dict)
    ]
    url = (
        f"cardea://env.try/{output['commit']}?receipt={quote(receipt_id)}"
        f"&scenarios={','.join(scenarios)}"
    )
    return {
        "rel": REL,
        "url": url,
        "external_id": key,
        "observed_state": "pass" if output.get("passed") is True else "fail",
        "observed_at": now().isoformat(),
    }


def attach(
    conn: sa.Connection,
    task_id: str,
    namespace: str,
    record_id: str,
    output: dict[str, JsonValue],
    *,
    receipt_id: str,
    idempotency_context: str | None,
    caller: str,
) -> None:
    """Add the run's link to the record, unless one with this call's key is already there."""
    row = authorize(conn, task_id, namespace, record_id)
    # A cached result names the run that produced it; that receipt is what is evidence.
    produced = str(output.get("cached_receipt_id") or receipt_id)
    entry = link(output, produced, idempotency_context or produced)
    links = json.loads(row["links_json"])
    if any(
        item.get("rel") == REL and item.get("external_id") == entry["external_id"] for item in links
    ):
        return
    if len(links) >= MAX_LINKS:
        raise OperationRejected(
            f"the record already has {MAX_LINKS} links; none was added", "record_invalid"
        )
    old = record_view(row)
    at = now().isoformat()
    changed = conn.exec_driver_sql(
        "UPDATE records SET links_json=?,version=version+1,updated_at=? "
        "WHERE namespace=? AND collection=? AND record_id=? AND version=?",
        (
            json.dumps([*links, entry]),
            at,
            namespace,
            row["collection"],
            record_id,
            row["version"],
        ),
    ).rowcount
    assert changed == 1  # the write lock is held, so the version cannot move under us
    new = record_view(_record(conn, namespace, record_id))
    detail: dict[str, JsonValue] = {"attached": REL, "receipt_id": produced}
    append_history(conn, namespace, "updated", caller, task_id, receipt_id, old, new, detail)
    journal(conn, namespace, "updated", new, caller, task_id, datetime.fromisoformat(at), detail)
