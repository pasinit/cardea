"""Host-owner maintenance using SQLite snapshots and explicit, redacted metadata."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from cardea.kernel.models import ObjectVersion, OperationReceipt, ResourceRef, Task, TaskState
from cardea.kernel.store import APPLICATION_ID, STORAGE_VERSION
from cardea.service.database import SCHEMA_HEAD, Fault, service_lock
from cardea.service.models import ServiceConfig
from cardea.service.profiles import runtime_ids

SCHEMAS = {"m1_001", "m2_001", "m3_001", "m4_001", "m5_001", "m6_001", "m6_002", "m6_003"}
RUNTIME = "dbos=3.0.0;workflow=cardea-m1-v1"
HOST_INPUT_RUNTIMES = runtime_ids(lambda p: p.host_inputs)
GENERATED_BACKUP = re.compile(r"before-(?:migration|runtime|restore)-[0-9a-f]{32}\.sqlite3")


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def private_descriptor(path: Path) -> Iterator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Fault("unsafe_backup", "File must be private, regular and owned", 500)
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def reader(path: Path) -> Iterator[sqlite3.Connection]:
    # Validate the private path before SQLite opens it. The enclosing service directory
    # is private; restore additionally copies a held source descriptor into staging.
    with (
        private_descriptor(path),
        closing(sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)) as conn,
    ):
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn


def compatible(conn: sqlite3.Connection) -> str:
    if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise Fault("invalid_backup", "Wrong database application identity", 400)
    if conn.execute("PRAGMA user_version").fetchone()[0] != STORAGE_VERSION:
        raise Fault("invalid_backup", "Unsupported base schema", 400)
    row = conn.execute("SELECT version_num FROM cardea_alembic_version").fetchone()
    if row is None or row[0] not in SCHEMAS:
        raise Fault("unsupported_schema", "Unknown service schema version", 400)
    runtime = conn.execute("SELECT value FROM service_meta WHERE name='runtime_version'").fetchone()
    if runtime and runtime[0] != RUNTIME:
        raise Fault(
            "unsupported_runtime", "Runtime upgrade requires an explicit compatibility path", 400
        )
    return str(row[0])


def validate(conn: sqlite3.Connection) -> dict[str, Any]:
    revision = compatible(conn)
    if [tuple(row) for row in conn.execute("PRAGMA integrity_check")] != [("ok",)]:
        raise Fault("invalid_backup", "Database integrity check failed", 400)
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise Fault("invalid_backup", "Database referential integrity failed", 400)
    count = 0
    for row in conn.execute("SELECT namespace,resource_id,version,content,digest FROM objects"):
        if hashlib.sha256(bytes(row[3])).hexdigest() != row[4]:
            raise Fault("artifact_corrupt", "Stored artifact digest does not match", 400)
        count += 1

    def reference(value: ResourceRef) -> None:
        if isinstance(value, ObjectVersion):
            row = conn.execute(
                "SELECT digest FROM objects WHERE namespace=? AND resource_id=? AND version=?",
                (value.namespace, value.resource_id, value.version),
            ).fetchone()
            valid = row is not None and row[0] == value.content_digest
        else:
            valid = (
                conn.execute(
                    "SELECT 1 FROM objects WHERE namespace=? AND resource_id=?",
                    (value.namespace, value.resource_id),
                ).fetchone()
                is not None
            )
        if not valid:
            raise Fault(
                "artifact_reference_invalid", "Referenced artifact is missing or changed", 400
            )

    try:
        ref: ResourceRef
        tasks = 0
        for row in conn.execute("SELECT task_id,revision,state,snapshot FROM tasks"):
            task = Task.model_validate_json(row[3])
            if (task.task_id, task.revision, task.state.value) != tuple(row[:3]):
                raise Fault("invalid_backup", "Task index and snapshot disagree", 400)
            if task.checkpoint:
                for ref in task.checkpoint.input_versions:
                    reference(ref)
            if task.verification:
                for ref in (*task.verification.artifacts, *task.verification.evidence):
                    reference(ref)
            if task.runtime_id in HOST_INPUT_RUNTIMES:
                binding = conn.execute(
                    "SELECT request_json FROM task_bindings WHERE task_id=?", (task.task_id,)
                ).fetchone()
                if binding:
                    request = json.loads(binding[0])
                    if "input_object" in request:
                        reference(ObjectVersion.model_validate(request["input_object"]))
            tasks += 1
        for row in conn.execute(
            "SELECT task_id,operation_id,state,revision,receipt_id,snapshot FROM operations"
        ):
            receipt = OperationReceipt.model_validate_json(row[5])
            if (
                receipt.request.task_id,
                receipt.request.operation_id,
                receipt.state.value,
                receipt.revision,
                receipt.receipt_id,
            ) != tuple(row[:5]):
                raise Fault("invalid_backup", "Operation index and snapshot disagree", 400)
            for ref in receipt.reconciliation_evidence:
                reference(ref)
            if (
                receipt.request.name == "objects.write"
                and receipt.result
                and "artifact" in receipt.result
            ):
                reference(ObjectVersion.model_validate(receipt.result["artifact"]))
    except (ValidationError, ValueError, TypeError, RecursionError) as error:
        raise Fault("invalid_backup", "Stored contract validation failed", 400) from error
    return {"schema": revision, "tasks": tasks, "objects": count, "integrity": "ok"}


def verify(directory: Path) -> dict[str, Any]:
    with service_lock(directory), reader(directory / "kernel.sqlite3") as conn:
        if (directory / ".restore-in-progress").exists() or (
            directory / ".restore-in-progress"
        ).is_symlink():
            raise Fault("restore_incomplete", "Complete the interrupted restore", 503)
        return validate(conn)


def diagnostics(config: ServiceConfig) -> dict[str, Any]:
    """Never include raw exceptions, paths, providers, actor IDs, payloads or journals."""
    result: dict[str, Any] = {
        "format": "cardea-diagnostics-v1",
        "versions": {name: version(name) for name in ("dbos", "alembic", "a2a-sdk", "mcp")},
        "profiles": {
            "host_configured": config.host_execution,
            "model_configured": config.agent_runtime is not None,
        },
        "checks": {},
    }
    checks = result["checks"]
    try:
        if (config.data_dir / ".restore-in-progress").exists() or (
            config.data_dir / ".restore-in-progress"
        ).is_symlink():
            raise Fault("restore_incomplete", "Pending restore", 503)
        with reader(config.data_dir / "kernel.sqlite3") as conn:
            revision = compatible(conn)
            checks["schema"] = "ok" if revision == SCHEMA_HEAD else "migration_required"
            result["task_counts"] = {
                state.value: conn.execute(
                    "SELECT count(*) FROM tasks WHERE state=?", (state.value,)
                ).fetchone()[0]
                for state in TaskState
            }
            if revision == SCHEMA_HEAD:
                row = conn.execute(
                    "SELECT epoch FROM recovery_epochs ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                # Only the expected generated UUID format is allowed into a support bundle.
                if row and not re.fullmatch("[0-9a-f]{32}", row[0]):
                    raise Fault("invalid_recovery_epoch", "Invalid recovery epoch", 500)
                result["new_namespace_prefix"] = f"r{row[0]}." if row else None
        checks["database"] = "readable"
    except Fault as error:
        checks["database"] = error.code
    except Exception:
        checks["database"] = "unavailable"
    try:
        with private_descriptor(config.data_dir / "service.lock") as descriptor:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                checks["service"] = "running"
            else:
                checks["service"] = "stopped"
    except Exception:
        checks["service"] = "unavailable"
    if config.host_execution:
        from cardea.hosts.debian import DebianHost

        try:
            DebianHost().probe()
            checks["host"] = "verified"
        except Exception:
            checks["host"] = "unavailable"
    else:
        checks["host"] = "disabled"
    result["ready"] = (
        checks.get("schema") == "ok"
        and checks.get("database") == "readable"
        and checks.get("service") == "running"
        and checks["host"] in {"disabled", "verified"}
    )
    return result


def prune_backups(
    directory: Path, *, keep: int = 3, days: int = 7, apply: bool = False
) -> dict[str, Any]:
    if keep < 1 or days < 1:
        raise Fault("invalid_retention", "Keep and age must be positive", 400)
    with service_lock(directory):
        if (directory / ".restore-in-progress").exists() or (
            directory / ".restore-in-progress"
        ).is_symlink():
            raise Fault("restore_incomplete", "Do not prune during incomplete recovery", 503)
        candidates = []
        for path in directory.iterdir():
            if GENERATED_BACKUP.fullmatch(path.name):
                with private_descriptor(path) as descriptor:
                    info = os.fstat(descriptor)
                    candidates.append((info.st_mtime, path.name, info.st_ino, info.st_dev))
        candidates.sort(reverse=True)
        selected = [row for row in candidates[keep:] if row[0] < time.time() - days * 86400]
        if apply:
            for _, name, inode, device in selected:
                path = directory / name
                with private_descriptor(path) as descriptor:
                    info = os.fstat(descriptor)
                    if (info.st_ino, info.st_dev) != (inode, device):
                        raise Fault(
                            "backup_changed", "Backup changed during retention planning", 409
                        )
                    path.unlink()
            sync_directory(directory)
        return {
            "apply": apply,
            "eligible": len(selected),
            "retained": len(candidates) - len(selected),
            "backups": [row[1] for row in selected],
        }
