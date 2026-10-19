"""Trusted local persistence, not an agent-facing authorization boundary.

All changes within a transaction commit with their journal entries. Small object
bytes live in SQLite too, avoiding a second filesystem commit protocol for now.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Concatenate
from uuid import uuid4

from pydantic import TypeAdapter

from cardea.kernel.lifecycle import (
    ContractError,
    ContractErrorCode,
    match_operation_retry,
    transition_operation,
    transition_task,
)
from cardea.kernel.models import (
    Checkpoint,
    DispatchLease,
    Document,
    EventWait,
    KernelEvent,
    KernelFailure,
    ObjectVersion,
    OperationReceipt,
    OperationRequest,
    OperationState,
    ResourceRef,
    ResourceUsage,
    RetryClass,
    Task,
    TaskState,
    TimerWait,
    Timestamp,
    VerificationRecord,
    WaitCondition,
)

APPLICATION_ID = 0x43415244
STORAGE_VERSION = 1
MAX_OBJECT_BYTES = 1024 * 1024
MAX_PAGE_SIZE = 1000
_timestamp = TypeAdapter(Timestamp)

_MIGRATION_1 = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        state TEXT NOT NULL, snapshot TEXT NOT NULL)""",
    """CREATE TABLE operations (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), operation_id TEXT NOT NULL,
        receipt_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, revision INTEGER NOT NULL,
        snapshot TEXT NOT NULL, PRIMARY KEY (task_id, operation_id))""",
    """CREATE TABLE dispatch_leases (
        lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id))""",
    """CREATE TABLE objects (
        namespace TEXT NOT NULL, resource_id TEXT NOT NULL, version INTEGER NOT NULL,
        content BLOB NOT NULL, digest TEXT NOT NULL,
        PRIMARY KEY (namespace, resource_id, version))""",
    """CREATE TABLE events (
        cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
        namespace TEXT NOT NULL, event_type TEXT NOT NULL, producer_id TEXT NOT NULL,
        task_id TEXT REFERENCES tasks(task_id), correlation_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL, payload TEXT NOT NULL)""",
    "CREATE INDEX events_by_task ON events(task_id, cursor)",
    "CREATE INDEX events_by_scope ON events(namespace, event_type, cursor)",
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
)


class StoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _private_path(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StoreError("unsafe_store_path", "Store paths must be private and owned by this user.")


class SQLiteKernelStore:
    def __init__(
        self,
        directory: Path,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_path(directory, directory=True)
        self.path = directory / "kernel.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _private_path(self.path, directory=False)
        else:
            os.close(descriptor)
        self._fault = fault or (lambda stage: None)
        self._active = False
        self._closed = False
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        try:
            self._migrate()
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            self._connection.close()
            raise

    def _migrate(self) -> None:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            application = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if application == APPLICATION_ID and version == STORAGE_VERSION:
                connection.commit()
                return
            tables = connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            if application != 0 or version != 0 or tables is not None:
                raise StoreError(
                    "unsupported_store", "Unknown database identity or schema version."
                )
            for statement in _MIGRATION_1:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {STORAGE_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[KernelTransaction]:
        if self._closed or self._active:
            raise StoreError("invalid_transaction", "Store is closed or already in a transaction.")
        self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        self._active = True
        transaction = KernelTransaction(self._connection, write=write, fault=self._fault)
        try:
            yield transaction
            if transaction.failed:
                raise StoreError(
                    "transaction_aborted", "A mutation failed; transaction rolled back."
                )
            if write:
                self._fault("before_commit")
            self._connection.commit()
            if write:
                self._fault("after_commit")
        except BaseException:
            self._connection.rollback()
            raise
        finally:
            transaction.active = False
            self._active = False

    def close(self) -> None:
        if self._active:
            raise StoreError("invalid_transaction", "Cannot close an active transaction.")
        self._connection.close()
        self._closed = True

    def __enter__(self) -> SQLiteKernelStore:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _mutation[**P, T](
    function: Callable[Concatenate[KernelTransaction, P], T],
) -> Callable[Concatenate[KernelTransaction, P], T]:
    @wraps(function)
    def wrapped(self: KernelTransaction, /, *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            self._check(write=True)
            return function(self, *args, **kwargs)
        except BaseException:
            # Even a caller that catches this error cannot commit preceding partial writes.
            self.failed = True
            raise

    return wrapped


def _expect_revision(actual: int, expected: int) -> None:
    if type(expected) is not int or actual != expected:
        raise ContractError(ContractErrorCode.REVISION_CONFLICT, "Task revision has changed.")


class KernelTransaction:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        write: bool,
        fault: Callable[[str], None],
    ) -> None:
        self._connection = connection
        self._write = write
        self._fault = fault
        self.active = True
        self.failed = False

    def _check(self, *, write: bool = False) -> None:
        if not self.active or self.failed or (write and not self._write):
            raise StoreError("invalid_transaction", "Transaction is closed, failed, or read-only.")

    def get_task(self, task_id: str) -> Task:
        self._check()
        row = self._connection.execute(
            "SELECT snapshot FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise StoreError("task_not_found", "Task does not exist.")
        return Task.model_validate_json(row["snapshot"])

    @_mutation
    def submit_task(self, task: Task) -> Task:
        task = Task.model_validate(task.model_dump())
        if (
            task.state is not TaskState.QUEUED
            or task.revision != 0
            or task.checkpoint is not None
            or task.created_at != task.updated_at
        ):
            raise StoreError("invalid_submission", "A new task must start queued at revision zero.")
        try:
            self._connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?)",
                (task.task_id, task.revision, task.state.value, task.model_dump_json()),
            )
        except sqlite3.IntegrityError as error:
            raise StoreError("task_exists", "Task ID already exists.") from error
        self._fault("task_written")
        self._task_event(task, "task.submitted")
        return task

    def _save_task(self, before: Task, after: Task, event_type: str) -> Task:
        result = self._connection.execute(
            "UPDATE tasks SET revision = ?, state = ?, snapshot = ? "
            "WHERE task_id = ? AND revision = ?",
            (
                after.revision,
                after.state.value,
                after.model_dump_json(),
                before.task_id,
                before.revision,
            ),
        )
        if result.rowcount != 1:
            raise ContractError(ContractErrorCode.REVISION_CONFLICT, "Task revision has changed.")
        self._fault("task_written")
        self._task_event(after, event_type)
        return after

    def _task_event(self, task: Task, event_type: str) -> KernelEvent:
        return self.append_event(
            namespace="kernel.tasks",
            event_type=event_type,
            producer_id="kernel",
            correlation_id=task.task_id,
            task_id=task.task_id,
            at=task.updated_at,
            payload={"revision": task.revision, "state": task.state.value},
        )

    def _require_lease(self, task: Task, lease_id: str | None, at: datetime) -> None:
        now = _timestamp.validate_python(at)
        if task.state is not TaskState.RUNNING or task.lease is None:
            raise StoreError("task_not_running", "Task has no live worker.")
        if task.lease.lease_id != lease_id or now >= task.lease.expires_at:
            raise StoreError("stale_lease", "Worker lease is wrong or expired.")
        if now < task.updated_at:
            raise ContractError(ContractErrorCode.TIME_REGRESSION, "Worker predates task state.")

    def _unresolved(self, task_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM operations WHERE task_id = ? "
            "AND state IN ('accepted', 'running', 'outcome_unknown') LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None

    @_mutation
    def change_task(
        self,
        task_id: str,
        target: TaskState,
        *,
        expected_revision: int,
        at: datetime,
        lease_id: str | None = None,
        lease: DispatchLease | None = None,
        checkpoint: Checkpoint | None = None,
        wait: WaitCondition | None = None,
        verification: VerificationRecord | None = None,
        failure: KernelFailure | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        _expect_revision(task.revision, expected_revision)
        if task.state is TaskState.RUNNING:
            self._require_lease(task, lease_id, at)
        if self._unresolved(task_id):
            raise StoreError(
                "unresolved_operations", "Settle operations before changing task state."
            )
        if checkpoint is not None:
            if checkpoint.outstanding_operation_ids:
                raise StoreError("invalid_checkpoint", "This store checkpoints only settled work.")
            for reference in checkpoint.input_versions:
                self.read_object(reference)
        if verification is not None:
            for artifact in verification.artifacts:
                self.read_object(artifact)
            for evidence in verification.evidence:
                self.latest_object(evidence)
        updated = transition_task(
            task,
            target,
            expected_revision=expected_revision,
            at=at,
            checkpoint=checkpoint,
            wait=wait,
            lease=lease,
            verification=verification,
            failure=failure,
        )
        if updated.lease is not None:
            if updated.lease.acquired_at != _timestamp.validate_python(at):
                raise StoreError("invalid_lease", "A dispatch lease must start at admission time.")
            try:
                self._connection.execute(
                    "INSERT INTO dispatch_leases VALUES (?, ?)",
                    (updated.lease.lease_id, task_id),
                )
            except sqlite3.IntegrityError as error:
                raise StoreError("lease_reused", "Dispatch lease IDs cannot be reused.") from error
        updated = self._save_task(task, updated, "task.transitioned")
        # Registering a wait and observing an already-arrived event share the same transaction.
        if updated.state is TaskState.WAITING and self._wait_ready(updated, at):
            return self.wake_task(task_id, expected_revision=updated.revision, at=at)
        return updated

    def _wait_ready(self, task: Task, at: datetime) -> bool:
        wait = task.wait
        if isinstance(wait, TimerWait):
            return _timestamp.validate_python(at) >= wait.wake_at
        if isinstance(wait, EventWait):
            return (
                self._connection.execute(
                    "SELECT 1 FROM events WHERE namespace = ? AND event_type = ? "
                    "AND cursor > ? LIMIT 1",
                    (wait.namespace, wait.event_type, wait.after_cursor),
                ).fetchone()
                is not None
            )
        return False

    @_mutation
    def wake_task(self, task_id: str, *, expected_revision: int, at: datetime) -> Task:
        task = self.get_task(task_id)
        _expect_revision(task.revision, expected_revision)
        if task.state is not TaskState.WAITING or not self._wait_ready(task, at):
            raise StoreError("wait_not_ready", "No matching event or elapsed timer.")
        updated = transition_task(
            task, TaskState.QUEUED, expected_revision=expected_revision, at=at
        )
        return self._save_task(task, updated, "task.woken")

    @_mutation
    def recover_task(self, task_id: str, *, expected_revision: int, at: datetime) -> Task:
        """Fence an expired worker; never dispatch or assume an external effect succeeded."""
        task = self.get_task(task_id)
        now = _timestamp.validate_python(at)
        _expect_revision(task.revision, expected_revision)
        if task.lease is None or now < task.lease.expires_at:
            raise StoreError("lease_not_expired", "Recovery requires an expired running lease.")
        rows = self._connection.execute(
            "SELECT snapshot FROM operations WHERE task_id = ? "
            "AND state IN ('accepted', 'running')",
            (task_id,),
        ).fetchall()
        for row in rows:
            receipt = OperationReceipt.model_validate_json(row["snapshot"])
            running = receipt.state is OperationState.RUNNING
            # A read-only operation has no external effect by declaration, so an interrupted
            # one failed rather than leaving an effect to reconcile.
            target = (
                OperationState.CANCELLED
                if not running
                else OperationState.FAILED
                if receipt.retry_class is RetryClass.READ_ONLY
                else OperationState.OUTCOME_UNKNOWN
            )
            updated = transition_operation(
                receipt,
                target,
                expected_revision=receipt.revision,
                at=now,
                failure=KernelFailure(code="worker_lost", message="Worker lease expired.")
                if running
                else None,
            )
            self._save_operation(receipt, updated)
        reason: KernelFailure | None = None
        if self._unresolved(task_id):
            reason = KernelFailure(
                code="reconciliation_required",
                message="An interrupted effect has unknown outcome.",
            )
        elif now >= task.limits.deadline:
            reason = KernelFailure(code="deadline_exceeded", message="Task deadline elapsed.")
        elif task.checkpoint is None:
            completed = self._connection.execute(
                "SELECT 1 FROM operations WHERE task_id = ? AND state = 'succeeded' LIMIT 1",
                (task_id,),
            ).fetchone()
            if completed is not None:
                reason = KernelFailure(
                    code="checkpoint_missing",
                    message="Completed work has no resumable checkpoint.",
                )
        # This exceptional running -> queued transition is available only after fencing and
        # reconciling all persisted operation intents under the same write transaction.
        updated_task = Task.model_validate(
            task.model_dump()
            | {
                "state": TaskState.FAILED if reason else TaskState.QUEUED,
                "lease": None,
                "revision": task.revision + 1,
                "updated_at": now,
                "failure": reason,
            }
        )
        return self._save_task(task, updated_task, "task.recovered")

    def get_operation(self, task_id: str, operation_id: str) -> OperationReceipt:
        self._check()
        row = self._connection.execute(
            "SELECT snapshot FROM operations WHERE task_id = ? AND operation_id = ?",
            (task_id, operation_id),
        ).fetchone()
        if row is None:
            raise StoreError("operation_not_found", "Operation does not exist.")
        return OperationReceipt.model_validate_json(row["snapshot"])

    def list_operations(self, task_id: str) -> list[OperationReceipt]:
        """Every receipt of one task, ordered by operation identifier."""
        self._check()
        rows = self._connection.execute(
            "SELECT snapshot FROM operations WHERE task_id = ? ORDER BY operation_id",
            (task_id,),
        ).fetchall()
        return [OperationReceipt.model_validate_json(row["snapshot"]) for row in rows]

    @_mutation
    def admit_operation(
        self,
        request: OperationRequest,
        *,
        grant_ids: tuple[str, ...],
        retry_class: RetryClass,
        expected_task_revision: int,
        lease_id: str,
        at: datetime,
    ) -> OperationReceipt:
        request = OperationRequest.model_validate(request.model_dump())
        existing = self._connection.execute(
            "SELECT snapshot FROM operations WHERE task_id = ? AND operation_id = ?",
            (request.task_id, request.operation_id),
        ).fetchone()
        if existing is not None:
            return match_operation_retry(OperationReceipt.model_validate_json(existing[0]), request)
        task = self.get_task(request.task_id)
        self._require_lease(task, lease_id, at)
        _expect_revision(task.revision, expected_task_revision)
        if request.subject_id != task.agent_id or request.deadline > task.limits.deadline:
            raise StoreError("request_scope_mismatch", "Request identity or deadline exceeds task.")
        receipt = OperationReceipt(
            receipt_id=f"receipt-{uuid4().hex}",
            request=request,
            request_digest=request.digest(),
            grant_ids=grant_ids,
            retry_class=retry_class,
            created_at=at,
            updated_at=at,
        )
        self._connection.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
            (
                request.task_id,
                request.operation_id,
                receipt.receipt_id,
                receipt.state.value,
                receipt.revision,
                receipt.model_dump_json(),
            ),
        )
        self._fault("operation_written")
        self._operation_event(receipt)
        return receipt

    def _save_operation(
        self,
        before: OperationReceipt,
        after: OperationReceipt,
    ) -> OperationReceipt:
        result = self._connection.execute(
            "UPDATE operations SET state = ?, revision = ?, snapshot = ? "
            "WHERE task_id = ? AND operation_id = ? AND revision = ?",
            (
                after.state.value,
                after.revision,
                after.model_dump_json(),
                before.request.task_id,
                before.request.operation_id,
                before.revision,
            ),
        )
        if result.rowcount != 1:
            raise ContractError(
                ContractErrorCode.REVISION_CONFLICT, "Receipt revision has changed."
            )
        self._fault("operation_written")
        self._operation_event(after)
        return after

    def _operation_event(self, receipt: OperationReceipt) -> KernelEvent:
        return self.append_event(
            namespace="kernel.operations",
            event_type="operation.transitioned",
            producer_id="kernel",
            task_id=receipt.request.task_id,
            correlation_id=receipt.request.operation_id,
            at=receipt.updated_at,
            payload={
                "receipt_id": receipt.receipt_id,
                "revision": receipt.revision,
                "state": receipt.state.value,
                "request_digest": receipt.request_digest,
            },
        )

    @_mutation
    def change_operation(
        self,
        task_id: str,
        operation_id: str,
        target: OperationState,
        *,
        expected_revision: int,
        lease_id: str,
        at: datetime,
        result: Document | None = None,
        failure: KernelFailure | None = None,
        usage: ResourceUsage | None = None,
    ) -> OperationReceipt:
        self._require_lease(self.get_task(task_id), lease_id, at)
        before = self.get_operation(task_id, operation_id)
        after = transition_operation(
            before,
            target,
            expected_revision=expected_revision,
            at=at,
            result=result,
            failure=failure,
            usage=usage,
        )
        return self._save_operation(before, after)

    @_mutation
    def reconcile_operation(
        self,
        task_id: str,
        operation_id: str,
        target: OperationState,
        *,
        expected_revision: int,
        at: datetime,
        evidence: tuple[ResourceRef, ...],
        result: Document | None = None,
        failure: KernelFailure | None = None,
    ) -> OperationReceipt:
        before = self.get_operation(task_id, operation_id)
        if before.state is not OperationState.OUTCOME_UNKNOWN:
            raise StoreError("not_uncertain", "Only uncertain operations can be reconciled.")
        for reference in evidence:
            self.latest_object(reference)
        after = transition_operation(
            before,
            target,
            expected_revision=expected_revision,
            at=at,
            reconciliation_evidence=evidence,
            result=result,
            failure=failure,
        )
        return self._save_operation(before, after)

    @_mutation
    def put_object(
        self,
        reference: ResourceRef,
        content: bytes,
        *,
        expected_version: int,
        producer_id: str,
        at: datetime,
        task_id: str | None = None,
        lease_id: str | None = None,
    ) -> ObjectVersion:
        if task_id is not None:
            self._require_lease(self.get_task(task_id), lease_id, at)
        reference = ResourceRef.model_validate(reference.model_dump())
        if type(expected_version) is not int or expected_version < 0:
            raise StoreError("invalid_version", "Expected version must be a nonnegative integer.")
        if not isinstance(content, bytes) or len(content) > MAX_OBJECT_BYTES:
            raise StoreError("object_too_large", "Objects must be bytes of at most one MiB.")
        row = self._connection.execute(
            "SELECT MAX(version) FROM objects WHERE namespace = ? AND resource_id = ?",
            (reference.namespace, reference.resource_id),
        ).fetchone()
        current = row[0] or 0
        if expected_version != current:
            raise StoreError("object_version_conflict", "Object version has changed.")
        version = ObjectVersion(
            **reference.model_dump(),
            version=current + 1,
            content_digest=sha256(content).hexdigest(),
        )
        self._connection.execute(
            "INSERT INTO objects VALUES (?, ?, ?, ?, ?)",
            (
                version.namespace,
                version.resource_id,
                version.version,
                content,
                version.content_digest,
            ),
        )
        self._fault("object_written")
        self.append_event(
            namespace=reference.namespace,
            event_type="object.written",
            producer_id=producer_id,
            task_id=task_id,
            correlation_id=reference.resource_id,
            at=at,
            payload={
                "resource_id": reference.resource_id,
                "version": version.version,
                "content_digest": version.content_digest,
            },
        )
        return version

    def latest_object(self, reference: ResourceRef) -> ObjectVersion:
        self._check()
        row = self._connection.execute(
            "SELECT version, digest FROM objects WHERE namespace = ? AND resource_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (reference.namespace, reference.resource_id),
        ).fetchone()
        if row is None:
            raise StoreError("object_not_found", "Object does not exist.")
        version = ObjectVersion(
            namespace=reference.namespace,
            resource_id=reference.resource_id,
            version=row["version"],
            content_digest=row["digest"],
        )
        self.read_object(version)
        return version

    def read_object(self, version: ObjectVersion) -> bytes:
        self._check()
        row = self._connection.execute(
            "SELECT content, digest FROM objects WHERE namespace = ? AND resource_id = ? "
            "AND version = ?",
            (version.namespace, version.resource_id, version.version),
        ).fetchone()
        if row is None:
            raise StoreError("object_not_found", "Object version does not exist.")
        content = bytes(row["content"])
        if (
            sha256(content).hexdigest() != version.content_digest
            or row["digest"] != version.content_digest
        ):
            raise StoreError("object_corrupt", "Stored content does not match its version digest.")
        return content

    @_mutation
    def append_event(
        self,
        *,
        namespace: str,
        event_type: str,
        producer_id: str,
        correlation_id: str,
        at: datetime,
        payload: Document | None = None,
        task_id: str | None = None,
    ) -> KernelEvent:
        event = KernelEvent(
            event_id=f"event-{uuid4().hex}",
            cursor=1,
            namespace=namespace,
            event_type=event_type,
            producer_id=producer_id,
            task_id=task_id,
            correlation_id=correlation_id,
            occurred_at=at,
            payload=payload or {},
        )
        cursor = self._connection.execute(
            "INSERT INTO events (event_id, namespace, event_type, producer_id, task_id, "
            "correlation_id, occurred_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.namespace,
                event.event_type,
                event.producer_id,
                event.task_id,
                event.correlation_id,
                event.occurred_at.isoformat(),
                json.dumps(event.payload),
            ),
        ).lastrowid
        return KernelEvent.model_validate(event.model_dump() | {"cursor": cursor})

    def read_events(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 100,
        task_id: str | None = None,
    ) -> tuple[KernelEvent, ...]:
        self._check()
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise StoreError("invalid_page", "Event page size must be between 1 and 1000.")
        if type(after_cursor) is not int or after_cursor < 0:
            raise StoreError("invalid_cursor", "Event cursor must be a nonnegative integer.")
        rows = self._connection.execute(
            "SELECT * FROM events WHERE cursor > ? AND (? IS NULL OR task_id = ?) "
            "ORDER BY cursor LIMIT ?",
            (after_cursor, task_id, task_id, limit),
        ).fetchall()
        return tuple(
            KernelEvent.model_validate(
                {
                    "event_id": row["event_id"],
                    "cursor": row["cursor"],
                    "namespace": row["namespace"],
                    "event_type": row["event_type"],
                    "producer_id": row["producer_id"],
                    "task_id": row["task_id"],
                    "correlation_id": row["correlation_id"],
                    "occurred_at": row["occurred_at"],
                    "payload": json.loads(row["payload"]),
                }
            )
            for row in rows
        )

    def latest_cursor(self) -> int:
        self._check()
        row = self._connection.execute("SELECT COALESCE(MAX(cursor), 0) FROM events").fetchone()
        return int(row[0])
