"""Run windows for read operations such as `env.try` (ADR-038 decisions 4 and 5).

A read operation may declare a `run_window`. Its run key is the SHA-256 of the RFC 8785
encoding of `{namespace, operation, key arguments, environment}`:

- a key argument that is a list of strings is sorted and deduplicated, so the same
  scenarios in another order are the same run;
- the environment digest covers the confined executor's profile digest (ADR-028) and, when
  `environment` names an argument, that repository's `environment` catalog in the host's
  repository registry (tiers, toolchain image digest, scenarios). Changing any of them is a
  different run.

At admission, in the intent's transaction:
- a `running` row that has not expired refuses the call (`env_run_in_progress`), so two
  identical calls never start two instances;
- a `settled` row inside the window answers with its result, `cached: true` and
  `cached_receipt_id`, and nothing runs (unless the call forces a rerun);
- a forced rerun is refused past `reruns_per_day` per subject in the preceding 24 hours
  (`env_rerun_limit`), and is recorded, linked to the earlier receipt;
- otherwise a `running` row is written for this receipt.

At settlement, success marks the row `settled`; a failure or an interrupted call removes it,
so the call can be made again. A row whose call outlived its deadline is ignored.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jsonpointer  # type: ignore[import-untyped]
import rfc8785
import sqlalchemy as sa
from pydantic import JsonValue

from cardea.kernel.store import KernelTransaction
from cardea.service import repositories
from cardea.service.database import Fault
from cardea.service.models import now
from cardea.service.operations import RunWindow

RERUN_PERIOD = timedelta(hours=24)
# A running row outlives its call's deadline by this much before it is ignored.
STALE_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class Decision:
    run_key: str
    environment_sha256: str
    # The earlier result to return instead of running, already marked `cached`.
    cached: dict[str, JsonValue] | None
    rerun_of: str | None
    rerun_subject: str | None


def _value(arguments: dict[str, JsonValue], pointer: str) -> JsonValue:
    try:
        value: JsonValue = jsonpointer.resolve_pointer(arguments, pointer)
    except jsonpointer.JsonPointerException:
        return None
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        unique: list[JsonValue] = [*sorted({str(v) for v in value})]
        return unique
    return value


def _digest(document: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(document)).hexdigest()


def environment_digest(
    window: RunWindow, arguments: dict[str, JsonValue], executor: dict[str, JsonValue] | None
) -> str:
    catalog = None
    if window.environment is not None:
        alias = _value(arguments, window.environment)
        registry = repositories.load()
        entry = registry.repositories.get(str(alias)) if registry is not None else None
        if entry is not None and entry.environment is not None:
            catalog = entry.environment.digest()
    pinned = (
        {k: executor.get(k) for k in ("profile", "worker", "profile_sha256")} if executor else None
    )
    return _digest({"executor": pinned, "environment": catalog})


def decide(
    conn: sa.Connection,
    tx: KernelTransaction,
    *,
    name: str,
    namespace: str,
    window: RunWindow,
    arguments: dict[str, JsonValue],
    executor: dict[str, JsonValue] | None,
) -> Decision:
    """Before the intent exists: refuse, answer from the window, or let the call run."""
    environment = environment_digest(window, arguments, executor)
    key = _digest(
        {
            "namespace": namespace,
            "operation": name,
            "key": [_value(arguments, pointer) for pointer in window.key],
            "environment": environment,
        }
    )
    at = now()
    row = (
        conn.exec_driver_sql(
            "SELECT state,task_id,operation_id,receipt_id,expires_at,settled_at FROM env_runs "
            "WHERE run_key=?",
            (key,),
        )
        .mappings()
        .first()
    )
    if row is not None and row["state"] == "running" and row["expires_at"] > at.isoformat():
        raise Fault(
            "env_run_in_progress", "An identical run is in progress; retry when it settles", 409
        )
    settled = (
        row is not None
        and row["state"] == "settled"
        and row["settled_at"] > (at - timedelta(seconds=window.seconds)).isoformat()
    )
    forced = window.rerun is not None and _value(arguments, window.rerun) is True
    if settled and not forced:
        assert row is not None
        earlier = tx.get_operation(str(row["task_id"]), str(row["operation_id"]))
        output = (earlier.result or {}).get("output")
        if isinstance(output, dict):
            cached: dict[str, JsonValue] = {
                **output,
                "cached": True,
                "cached_receipt_id": str(row["receipt_id"]),
            }
            return Decision(key, environment, cached, None, None)
    subject = None
    if forced:
        assert window.rerun_subject is not None
        subject = _digest(
            {
                "namespace": namespace,
                "operation": name,
                "subject": [_value(arguments, pointer) for pointer in window.rerun_subject],
            }
        )
        used = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM env_reruns WHERE namespace=? AND operation=? AND "
            "subject_sha256=? AND at>?",
            (namespace, name, subject, (at - RERUN_PERIOD).isoformat()),
        ).scalar_one()
        if int(used) >= window.reruns_per_day:
            raise Fault(
                "env_rerun_limit",
                f"At most {window.reruns_per_day} forced reruns per day for this subject",
                429,
            )
    rerun_of = str(row["receipt_id"]) if forced and row is not None else None
    return Decision(key, environment, None, rerun_of, subject)


def start(
    conn: sa.Connection,
    decision: Decision,
    *,
    name: str,
    namespace: str,
    task_id: str,
    operation_id: str,
    receipt_id: str,
    deadline: Any,
) -> None:
    """In the intent's transaction: this receipt now runs for the key."""
    at = now()
    conn.exec_driver_sql(
        "INSERT INTO env_runs (run_key,namespace,operation,state,task_id,operation_id,"
        "receipt_id,environment_sha256,started_at,expires_at,settled_at) "
        "VALUES (?,?,?,'running',?,?,?,?,?,?,NULL) ON CONFLICT(run_key) DO UPDATE SET "
        "state='running',task_id=excluded.task_id,operation_id=excluded.operation_id,"
        "receipt_id=excluded.receipt_id,environment_sha256=excluded.environment_sha256,"
        "started_at=excluded.started_at,expires_at=excluded.expires_at,settled_at=NULL",
        (
            decision.run_key,
            namespace,
            name,
            task_id,
            operation_id,
            receipt_id,
            decision.environment_sha256,
            at.isoformat(),
            (deadline + STALE_AFTER).isoformat(),
        ),
    )
    if decision.rerun_subject is not None:
        conn.exec_driver_sql(
            "INSERT INTO env_reruns (namespace,operation,subject_sha256,receipt_id,at) "
            "VALUES (?,?,?,?,?)",
            (namespace, name, decision.rerun_subject, receipt_id, at.isoformat()),
        )


def finish(conn: sa.Connection, task_id: str, operation_id: str, succeeded: bool) -> None:
    """In the settlement's transaction: keep a successful run; forget any other."""
    if succeeded:
        conn.exec_driver_sql(
            "UPDATE env_runs SET state='settled',settled_at=? WHERE task_id=? AND operation_id=?",
            (now().isoformat(), task_id, operation_id),
        )
    else:
        conn.exec_driver_sql(
            "DELETE FROM env_runs WHERE task_id=? AND operation_id=? AND state='running'",
            (task_id, operation_id),
        )
