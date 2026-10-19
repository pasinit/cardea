"""One SQLite file, native SQLAlchemy transactions and Alembic extension migrations."""

import fcntl
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from cardea.kernel.store import KernelTransaction, SQLiteKernelStore, StoreError


class Fault(Exception):
    def __init__(self, code: str, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# Latest Alembic revision this release runs against; older stores need migration.
SCHEMA_HEAD = "m6_003"


def private_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Fault("unsafe_file", "Credential/configuration file must be private", 500)
        if info.st_size > 262144:
            raise Fault("file_too_large", "Configuration or credential exceeds limit", 500)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(262145)
    finally:
        os.close(fd)


@contextmanager
def service_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("unsafe_directory", "Service directory must be private", 500)
    fd = os.open(directory / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Fault("service_running", "The store already has an active service", 409) from None
        yield
    finally:
        os.close(fd)


@contextmanager
def borrow(connection: sa.Connection | Session) -> Iterator[KernelTransaction]:
    owner = connection.connection() if isinstance(connection, Session) else connection
    raw = owner.connection.driver_connection
    if not isinstance(raw, sqlite3.Connection) or not raw.in_transaction:
        raise StoreError("invalid_transaction", "A caller-owned SQLite transaction is required")
    tx = KernelTransaction(raw, write=True, fault=lambda stage: None)
    try:
        yield tx
        if tx.failed:
            raise StoreError("transaction_aborted", "Failed kernel mutation aborted transaction")
    except BaseException:
        owner.rollback()
        raise
    finally:
        tx.active = False


class Database:
    def __init__(self, directory: Path) -> None:
        marker = directory / ".restore-in-progress"
        if marker.exists() or marker.is_symlink():
            raise Fault(
                "restore_incomplete", "Complete the interrupted restore before opening", 503
            )
        self.directory = directory
        # Validates private paths and the unmodified base schema identity/version.
        with SQLiteKernelStore(directory):
            pass
        self.path = directory / "kernel.sqlite3"
        self.url = f"sqlite:///{self.path}"
        self.engine = sa.create_engine(self.url)

        @sa.event.listens_for(self.engine, "connect")
        def configure(connection: sqlite3.Connection, record: Any) -> None:
            connection.isolation_level = None
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")

        @sa.event.listens_for(self.engine, "begin")
        def begin(connection: sa.Connection) -> None:
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.read_engine = sa.create_engine(
            f"sqlite:///file:{quote(str(self.path))}?mode=ro&uri=true"
        )

        @sa.event.listens_for(self.read_engine, "connect")
        def configure_reader(connection: sqlite3.Connection, record: Any) -> None:
            connection.isolation_level = None
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")

        @sa.event.listens_for(self.read_engine, "begin")
        def begin_reader(connection: sa.Connection) -> None:
            connection.exec_driver_sql("BEGIN")

    @contextmanager
    def read(self) -> Iterator[sa.Connection]:
        """Fresh, read-only WAL snapshot for policy and data in the same transaction."""
        with self.read_engine.begin() as connection:
            yield connection

    def migrate(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")

    def check_version(self) -> None:
        with self.engine.begin() as conn:
            try:
                revision = conn.exec_driver_sql(
                    "SELECT version_num FROM cardea_alembic_version"
                ).scalar_one()
            except sa.exc.SQLAlchemyError:
                raise Fault("migration_required", "Run service initialization first", 500) from None
            if revision != SCHEMA_HEAD:
                raise Fault("unsupported_schema", "Unknown service schema version", 500)

    def backup(self, destination: Path) -> None:
        # SQLite's online backup includes every Cardea/DBOS table at one consistent point.
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            with (
                closing(sqlite3.connect(self.path)) as source,
                closing(sqlite3.connect(destination)) as target,
            ):
                source.backup(target)
                # A transportable snapshot must not depend on a separate WAL file.
                target.execute("PRAGMA journal_mode=DELETE")
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise Fault("backup_failed", "Backup integrity check failed", 500)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def close(self) -> None:
        self.read_engine.dispose()
        self.engine.dispose()
