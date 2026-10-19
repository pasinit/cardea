"""Explicit host-owner initialization and crash-safe, execution-fenced recovery."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from cardea.kernel.models import KernelFailure, OperationReceipt, Task, TaskState
from cardea.service.database import SCHEMA_HEAD, Database, Fault, borrow, service_lock
from cardea.service.maintenance import (
    compatible,
    private_descriptor,
    reader,
    sync_directory,
    validate,
)
from cardea.service.models import now

TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


def initialize(directory: Path) -> Path | None:
    with service_lock(directory):
        marker = directory / ".restore-in-progress"
        if marker.exists() or marker.is_symlink():
            raise Fault("restore_incomplete", "Complete the interrupted restore", 503)
        existed = (directory / "kernel.sqlite3").exists()
        if existed:
            with reader(directory / "kernel.sqlite3") as conn:
                service = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cardea_alembic_version'"
                ).fetchone()
                if service and compatible(conn) == SCHEMA_HEAD:
                    return None
        database = Database(directory)
        snapshot = directory / f"before-migration-{uuid4().hex}.sqlite3" if existed else None
        try:
            if snapshot:
                database.backup(snapshot)
            database.migrate()
            database.check_version()
            return snapshot
        finally:
            database.close()


def backup(directory: Path, destination: Path) -> None:
    with service_lock(directory):
        database = Database(directory)
        try:
            database.check_version()
            database.backup(destination)
        finally:
            database.close()


def seal(database: Database, source_digest: str) -> str:
    epoch = uuid4().hex
    at = now()
    with database.engine.begin() as conn, borrow(conn) as tx:
        tasks = [
            Task.model_validate_json(row[0])
            for row in conn.exec_driver_sql("SELECT snapshot FROM tasks")
        ]
        receipts = [
            OperationReceipt.model_validate_json(row[0])
            for row in conn.exec_driver_sql("SELECT snapshot FROM operations")
        ]
        if any(task.updated_at > at for task in tasks) or any(
            receipt.updated_at > at for receipt in receipts
        ):
            raise Fault("recovery_clock_regressed", "Restore requires a non-regressing clock", 409)
        if any(task.lease and task.lease.expires_at > at for task in tasks):
            raise Fault("recovery_lease_live", "Wait for copied worker leases to expire", 409)
        for task in tasks:
            if task.state in TERMINAL:
                continue
            if task.lease:
                task = tx.recover_task(task.task_id, expected_revision=task.revision, at=at)
            if task.state not in TERMINAL:
                tx.change_task(
                    task.task_id,
                    TaskState.FAILED,
                    expected_revision=task.revision,
                    at=at,
                    failure=KernelFailure(
                        code="snapshot_restored",
                        message="Historical execution sealed by snapshot recovery.",
                    ),
                )
        conn.exec_driver_sql("UPDATE principals SET disabled=1")
        conn.exec_driver_sql("DELETE FROM policies")
        conn.exec_driver_sql(
            "INSERT INTO recovery_epochs VALUES (?,?,?)", (epoch, at.isoformat(), source_digest)
        )
        tx.append_event(
            namespace="service",
            event_type="service.restored",
            producer_id="host-owner",
            correlation_id=epoch,
            at=at,
            payload={"new_namespace_prefix": f"r{epoch}."},
        )
    return f"r{epoch}."


def copy_private(source: Path, destination: Path) -> None:
    with private_descriptor(source) as descriptor:
        copy_descriptor(descriptor, destination)


def copy_descriptor(descriptor: int, destination: Path) -> None:
    target = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(target, "wb") as output, os.fdopen(os.dup(descriptor), "rb") as original:
        original.seek(0)
        shutil.copyfileobj(original, output)
        output.flush()
        os.fsync(output.fileno())


def preserve_current(directory: Path) -> None:
    """Keep even a corrupt original and its WAL for host-owner forensic recovery."""
    if not (directory / "kernel.sqlite3").exists():
        return
    recovery = directory / f"before-restore-{uuid4().hex}"
    recovery.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm"):
        source = directory / f"kernel.sqlite3{suffix}"
        if source.exists() or source.is_symlink():
            copy_private(source, recovery / source.name)
    sync_directory(recovery)
    sync_directory(directory)


def restore(
    directory: Path, source: Path, *, fault: Callable[[str], None] = lambda stage: None
) -> dict[str, str]:
    with private_descriptor(source) as descriptor, service_lock(directory):
        if any(
            Path(str(source) + suffix).exists() or Path(str(source) + suffix).is_symlink()
            for suffix in ("-wal", "-shm")
        ):
            raise Fault("backup_has_sidecars", "Use a standalone completed SQLite snapshot", 400)
        target = directory / "kernel.sqlite3"
        if target.exists() and os.path.samestat(os.fstat(descriptor), target.stat()):
            raise Fault(
                "invalid_backup", "Use a standalone SQLite backup, not the active file", 400
            )
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=directory) as temporary:
            stage = Path(temporary)
            copy_descriptor(descriptor, stage / "kernel.sqlite3")
            with reader(stage / "kernel.sqlite3") as conn:
                validate(conn)
            with (stage / "kernel.sqlite3").open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            database = Database(stage)
            try:
                database.migrate()
                database.check_version()
                prefix = seal(database, digest)
                candidate = stage / "published.sqlite3"
                database.backup(candidate)
            finally:
                database.close()
            with reader(candidate) as conn:
                validate(conn)
            fault("validated")
            preserve_current(directory)
            marker = directory / ".restore-in-progress"
            if marker.exists() or marker.is_symlink():
                with private_descriptor(marker):
                    pass
            else:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"Restore must complete before this store can open.\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            sync_directory(directory)
            fault("marked")
            # The durable marker fences every opener while old WAL is removed and the
            # replacement is published. Never clear it on an interrupted publication.
            for suffix in ("-wal", "-shm"):
                (directory / f"kernel.sqlite3{suffix}").unlink(missing_ok=True)
            os.replace(candidate, target)
            sync_directory(directory)
            fault("published")
            marker.unlink()
            sync_directory(directory)
            return {"new_namespace_prefix": prefix, "source_sha256": digest}
