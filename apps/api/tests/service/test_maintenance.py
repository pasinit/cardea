"""Stopped-store atomicity, explicit retention and support-output redaction."""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from cardea.service import admin
from cardea.service.database import SCHEMA_HEAD, Database, Fault, service_lock
from cardea.service.maintenance import diagnostics, prune_backups, verify
from cardea.service.models import ServiceConfig


@pytest.mark.parametrize("stage", ["validated", "marked", "published"])
def test_interrupted_restore_is_fenced_and_repeatable(tmp_path: Path, stage: str) -> None:
    directory = tmp_path / "data"
    admin.initialize(directory)
    snapshot = tmp_path / "snapshot.sqlite3"
    admin.backup(directory, snapshot)
    original = (directory / "kernel.sqlite3").read_bytes()

    def crash(point: str) -> None:
        if point == stage:
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError):
        admin.restore(directory, snapshot, fault=crash)
    if stage == "validated":
        assert (directory / "kernel.sqlite3").read_bytes() == original
        assert not (directory / ".restore-in-progress").exists()
    else:
        with pytest.raises(Fault, match="interrupted restore"):
            Database(directory)
        with pytest.raises(Fault):
            admin.initialize(directory)
        with pytest.raises(Fault):
            prune_backups(directory, apply=True)
    admin.restore(directory, snapshot)
    assert verify(directory)["schema"] == SCHEMA_HEAD
    assert admin.initialize(directory) is None


def test_corrupt_destination_can_be_preserved_and_replaced(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    admin.initialize(directory)
    snapshot = tmp_path / "snapshot.sqlite3"
    admin.backup(directory, snapshot)
    (directory / "kernel.sqlite3").write_bytes(b"corrupt original")
    admin.restore(directory, snapshot)
    assert verify(directory)["integrity"] == "ok"
    retained = list(directory.glob("before-restore-*/kernel.sqlite3"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"corrupt original"


def test_runtime_and_schema_refusal_do_not_rewrite_store(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    admin.initialize(directory)
    with sqlite3.connect(directory / "kernel.sqlite3") as conn:
        conn.execute("INSERT OR REPLACE INTO service_meta VALUES ('runtime_version','future')")
    before = (directory / "kernel.sqlite3").read_bytes()
    with pytest.raises(Fault) as refused:
        admin.initialize(directory)
    assert refused.value.code == "unsupported_runtime"
    assert (directory / "kernel.sqlite3").read_bytes() == before
    assert not list(directory.glob("before-migration-*"))


def test_m5_migration_failure_rolls_back(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    database = Database(tmp_path / "data")
    config = Config()
    config.set_main_option("script_location", str(Path(admin.__file__).parent / "migrations"))
    with database.engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "m4_001")

    def fail(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if "CREATE TRIGGER recovery_epochs" in statement:
            raise RuntimeError("migration interrupted")

    sa.event.listen(database.engine, "before_cursor_execute", fail)
    with pytest.raises(RuntimeError):
        database.migrate()
    sa.event.remove(database.engine, "before_cursor_execute", fail)
    with database.read() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM cardea_alembic_version").scalar_one()
            == "m4_001"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='recovery_epochs'"
            ).first()
            is None
        )
    database.migrate()
    database.check_version()
    database.close()


def test_retention_is_explicit_age_and_count_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    admin.initialize(directory)
    for index in range(6):
        snapshot = directory / f"before-migration-{index:032x}.sqlite3"
        admin.backup(directory, snapshot)
        timestamp = time.time() - (index + 1) * 86400
        os.utime(snapshot, (timestamp, timestamp))
    external = directory / "user-backup.sqlite3"
    admin.backup(directory, external)
    before = (directory / "kernel.sqlite3").read_bytes()
    plan = prune_backups(directory, keep=2, days=4)
    assert plan["eligible"] == 3
    assert len(list(directory.glob("before-migration-*"))) == 6
    with service_lock(directory), pytest.raises(Fault):
        prune_backups(directory, apply=True)
    assert prune_backups(directory, keep=2, days=4, apply=True)["eligible"] == 3
    assert external.exists() and (directory / "kernel.sqlite3").read_bytes() == before
    link = directory / f"before-runtime-{'a' * 32}.sqlite3"
    link.symlink_to(external)
    with pytest.raises(OSError):
        prune_backups(directory, apply=True)
    assert external.exists()


def test_diagnostics_redacts_paths_ids_and_raw_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cardea.hosts.debian import DebianHost

    secret = "sensitive-canary-do-not-export"
    directory = tmp_path / secret
    admin.initialize(directory)
    config = ServiceConfig(
        data_dir=directory,
        spiffe_socket=f"unix:///{secret}",
        trust_domain="test",
        runtime_spiffe_id="spiffe://test/runtime",
        host_execution=True,
    )

    def failure(self: DebianHost) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(DebianHost, "probe", failure)
    with service_lock(directory):
        result = diagnostics(config)
    assert result["checks"]["service"] == "running"
    assert result["checks"]["host"] == "unavailable"
    assert not result["ready"]
    assert secret not in json.dumps(result)
    assert diagnostics(config)["checks"]["service"] == "stopped"


def test_sidecars_and_dangling_recovery_marker_fail_closed(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    admin.initialize(directory)
    snapshot = tmp_path / "snapshot.sqlite3"
    admin.backup(directory, snapshot)
    sidecar = Path(str(snapshot) + "-wal")
    sidecar.write_bytes(b"uncheckpointed history")
    with pytest.raises(Fault) as refused:
        admin.restore(directory, snapshot)
    assert refused.value.code == "backup_has_sidecars"
    sidecar.unlink()
    (directory / ".restore-in-progress").symlink_to(tmp_path / "absent")
    with pytest.raises(Fault) as refused:
        admin.initialize(directory)
    assert refused.value.code == "restore_incomplete"
    with pytest.raises(Fault):
        verify(directory)
    with pytest.raises(Fault):
        prune_backups(directory, apply=True)


def test_cli_invalid_configuration_never_prints_its_contents(tmp_path: Path) -> None:
    import subprocess
    import sys

    config = tmp_path / "invalid.json"
    config.write_text('{"data_dir": "credential-canary", "unknown": "secret-canary"}')
    config.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "-m", "cardea.service", "diagnostics", "--config", str(config)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert result.returncode == 1
    assert result.stdout == "" and result.stderr.strip() == "command_failed"
