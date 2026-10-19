import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.util.exc import CommandError
from pydantic import ValidationError

from cardea.service.admin import backup, initialize, restore
from cardea.service.database import Database, Fault, private_file, service_lock
from cardea.service.models import Principal, TaskRequest
from cardea.service.schema import schema


def test_migrate_populated_v01_without_rewriting_history(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "cardea.kernel.cli",
            "--directory",
            str(directory),
            "demo-start",
            "--task-id",
            "legacy",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    with sqlite3.connect(directory / "kernel.sqlite3") as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        before = {t: conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
    snapshot = initialize(directory)
    assert snapshot and snapshot.exists()
    with sqlite3.connect(directory / "kernel.sqlite3") as conn:
        assert {t: conn.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables} == before
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM task_bindings").fetchone() == (0,)
    database = Database(directory)
    try:
        database.check_version()
        with database.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE cardea_alembic_version SET version_num='future'")
        with pytest.raises(Fault, match="Unknown"):
            database.check_version()
        with pytest.raises(CommandError):
            database.migrate()
        with database.engine.begin() as conn:
            assert (
                conn.exec_driver_sql("SELECT version_num FROM cardea_alembic_version").scalar_one()
                == "future"
            )
    finally:
        database.close()


def test_interrupted_migration_rolls_back(tmp_path: Path) -> None:
    database = Database(tmp_path / "store")

    def interrupted(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if "CREATE TABLE task_bindings" in statement:
            raise RuntimeError("injected migration interruption")

    try:
        sa.event.listen(database.engine, "before_cursor_execute", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            database.migrate()
        sa.event.remove(database.engine, "before_cursor_execute", interrupted)
        with database.engine.begin() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE name='principals'"
                ).first()
                is None
            )
        database.migrate()
        database.check_version()
    finally:
        database.close()


def test_private_storage_owner_and_restore(tmp_path: Path) -> None:
    directory = tmp_path / "store"
    initialize(directory)
    with service_lock(directory), pytest.raises(Fault, match="active service"):
        initialize(directory)
    snapshot = tmp_path / "backup.sqlite3"
    backup(directory, snapshot)
    assert snapshot.stat().st_mode & 0o077 == 0
    snapshot.chmod(0o644)
    with pytest.raises(Fault, match="private"):
        restore(directory, snapshot)
    snapshot.chmod(0o600)
    restore(directory, snapshot)
    link = tmp_path / "link"
    link.symlink_to(snapshot)
    with pytest.raises(OSError):
        private_file(link)
    snapshot.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        restore(directory, snapshot)
    database = Database(directory)
    try:
        database.check_version()
    finally:
        database.close()


def test_jcs_version_and_reserved_identity() -> None:
    base: dict[str, Any] = dict(request_id="a", agent_id="a", namespace="a", objective="a")
    assert TaskRequest(**base, payload={"a": 1.0, "b": "é"}).digest("owner") == TaskRequest(
        **base, payload={"b": "é", "a": 1}
    ).digest("owner")
    assert TaskRequest(**base, payload={}).digest("owner") != TaskRequest(
        **base, payload={}
    ).digest("other")
    with pytest.raises(ValidationError):
        Principal(principal_id="platform", kind="human")


def test_exported_contract_is_separate_from_demo() -> None:
    document = schema()
    paths = document["paths"]
    assert isinstance(paths, dict)
    assert "/v0.2/tasks" in paths
    assert all(p.startswith("/v0.2/") for p in paths)


def test_error_contract_matches_served_errors() -> None:
    from typing import cast

    from fastapi.testclient import TestClient

    from cardea.service.app import create_app
    from cardea.service.kernel import Kernel
    from cardea.service.models import ErrorBody

    app = create_app(cast(Kernel, None))
    with TestClient(app) as client:
        for response in (
            client.get("/absent"),
            client.get("/v0.2/system"),
            client.post("/v0.2/tasks", content="x", headers={"Content-Encoding": "gzip"}),
        ):
            assert response.status_code in (401, 404, 415)
            assert ErrorBody.model_validate(response.json()).code
    contract = app.openapi()
    assert contract["paths"]["/v0.2/tasks"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorBody"}
    assert contract["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
