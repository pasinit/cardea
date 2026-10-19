"""Experiment-only composition of public SQLAlchemy transactions and Cardea mutations."""

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from cardea.kernel.lifecycle import ContractError, ContractErrorCode
from cardea.kernel.models import BudgetLimits, OperationRequest, ResourceRef, Task
from cardea.kernel.store import KernelTransaction, SQLiteKernelStore, StoreError
from dbos import DBOS, DBOSClient, EnqueueOptions
from sqlalchemy.orm import Session

TASK_ID = "integration-task"
WORKFLOW_ID = "cardea/integration-task/v1"
QUEUE = "cardea-fit"


def now() -> datetime:
    return datetime.now(UTC)


def barrier(root: Path, stage: str) -> None:
    if os.environ.get("FIT_BARRIER") != stage:
        return
    (root / f"{os.getpid()}.{stage}.entered").touch()
    deadline = time.monotonic() + 60
    while not (root / f"{os.getpid()}.{stage}.release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError(stage)
        time.sleep(0.02)


def url(root: Path, *, separate: bool = False) -> str:
    name = "engine.sqlite3" if separate else "kernel.sqlite3"
    return f"sqlite:///{root / name}"


def engine(root: Path) -> sa.Engine:
    result = sa.create_engine(url(root))

    @sa.event.listens_for(result, "connect")
    def connect(connection: sqlite3.Connection, record: Any) -> None:
        connection.isolation_level = None
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")

    @sa.event.listens_for(result, "begin")
    def begin(connection: sa.Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return result


@contextmanager
def borrow(connection: sa.Connection | Session) -> Iterator[KernelTransaction]:
    sql_connection = connection.connection() if isinstance(connection, Session) else connection
    raw = sql_connection.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
        raise StoreError("invalid_transaction", "Requires a real caller-owned SQLite transaction")
    transaction = KernelTransaction(raw, write=True, fault=lambda stage: None)
    try:
        yield transaction
        if transaction.failed:
            raise StoreError("transaction_aborted", "A caught mutation failure must roll back")
    except BaseException:
        # Poison even a caller that catches the context-manager error. Never commit here.
        sql_connection.rollback()
        raise
    finally:
        transaction.active = False


def initialize(root: Path, *, separate: bool = False) -> None:
    with SQLiteKernelStore(root):
        pass
    with sqlite3.connect(root / "kernel.sqlite3") as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fit_admissions (
                task_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fit_authority (
                supervisor_enabled INTEGER, supervisor_permission INTEGER,
                supervisor_delegation INTEGER, ancestor INTEGER,
                task_grant INTEGER, platform INTEGER);
        """)
        if not conn.execute("SELECT 1 FROM fit_authority").fetchone():
            conn.execute("INSERT INTO fit_authority VALUES (1,1,1,1,1,1)")
    with sqlite3.connect(root / "provider.sqlite3") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS effects (value TEXT, worker INTEGER)")
    DBOS(
        config={
            "name": "cardea-integration-fit",
            "system_database_url": url(root, separate=separate),
            "application_version": "fit-v1",
            "executor_id": "bootstrap",
            "enable_otlp": False,
            "log_level": "WARNING",
            "use_listen_notify": False,
        }
    )
    DBOS.launch()
    client = DBOSClient(system_database_url=url(root, separate=separate), use_listen_notify=False)
    client.register_queue(QUEUE, worker_concurrency=1, polling_interval_sec=0.05)
    client.destroy()
    DBOS.destroy(destroy_registry=True)


def request(value: int = 1) -> OperationRequest:
    # Stable fictional request across retries, including its deadline and v0.1 digest.
    return OperationRequest(
        operation_id="write-output",
        task_id=TASK_ID,
        subject_id="fit-agent",
        name="objects.write",
        resource=ResourceRef(namespace="fit", resource_id="output"),
        arguments={"value": value},
        deadline=datetime(2030, 1, 1, tzinfo=UTC),
    )


def task() -> Task:
    at = now()
    return Task(
        task_id=TASK_ID,
        agent_id="fit-agent",
        objective="Persist and verify fictional output",
        runtime_id="dbos-integration-fit-v1",
        verifier_id="fit-value-verifier-v1",
        capability_ids=("fictional-grant",),
        budget_account_id="fit-budget",
        limits=BudgetLimits(
            max_steps=20,
            max_model_calls=0,
            max_model_tokens=0,
            max_tool_milliseconds=10000,
            max_output_bytes=1024,
            max_concurrent_operations=1,
            deadline=request().deadline,
        ),
        created_at=at,
        updated_at=at,
    )


def admit(
    root: Path,
    value: int = 1,
    *,
    separate: bool = False,
    engine_first: bool = False,
    scenario: str = "local",
) -> str:
    client = DBOSClient(system_database_url=url(root, separate=separate), use_listen_notify=False)
    application = engine(root)
    req = request(value)
    options: EnqueueOptions = {
        "workflow_name": "integration",
        "queue_name": QUEUE,
        "workflow_id": WORKFLOW_ID,
    }
    try:
        if separate and engine_first:
            client.enqueue(options, str(root), req.model_dump_json(), scenario)
            barrier(root, "separate.engine_committed")
        with application.begin() as conn:
            existing = conn.exec_driver_sql(
                "SELECT digest FROM fit_admissions WHERE task_id=?", (TASK_ID,)
            ).fetchone()
            if existing:
                if existing[0] != req.digest():
                    raise ContractError(
                        ContractErrorCode.REQUEST_CONFLICT, "Admission input changed"
                    )
                return WORKFLOW_ID
            with borrow(conn) as tx:
                tx.submit_task(task())
                conn.exec_driver_sql(
                    "INSERT INTO fit_admissions VALUES (?,?,?)",
                    (TASK_ID, req.model_dump_json(), req.digest()),
                )
                barrier(root, "admission.written")
                if not separate:
                    client.enqueue_in_transaction(
                        conn, options, str(root), req.model_dump_json(), scenario
                    )
                    barrier(root, "enqueue.written")
            barrier(root, "admission.before_commit")
        barrier(root, "admission.after_commit")
        if separate and not engine_first:
            barrier(root, "separate.cardea_committed")
            client.enqueue(options, str(root), req.model_dump_json(), scenario)
        return WORKFLOW_ID
    finally:
        application.dispose()
        client.destroy()


def authority(conn: sa.Connection | Session) -> bool:
    # Fictional AND-policy only. No identity provider or production policy engine.
    row = conn.execute(sa.text("SELECT * FROM fit_authority")).one()
    return all(bool(value) for value in row)


def new_lease(worker: str) -> dict[str, Any]:
    at = now()
    return {
        "lease_id": "lease-" + uuid4().hex,
        "worker_id": worker,
        "acquired_at": at,
        "expires_at": at + timedelta(seconds=3),
    }
