"""The service clock never runs backwards within a process."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cardea.service import artifacts, models
from cardea.service.database import Database
from cardea.service.operations import OperationContext


def test_now_never_steps_back_when_the_wall_clock_does(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2030, 1, 1, tzinfo=UTC)
    readings = iter([start, start - timedelta(seconds=30), start + timedelta(seconds=1)])
    monkeypatch.setattr(models, "_latest", datetime.min.replace(tzinfo=UTC))
    monkeypatch.setattr(models, "_wall_clock", lambda: next(readings))
    assert models.now() == start
    assert models.now() == start
    assert models.now() == start + timedelta(seconds=1)


def test_now_follows_a_forward_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    first = datetime(2030, 1, 1, tzinfo=UTC)
    readings = iter([first, first + timedelta(minutes=1)])
    monkeypatch.setattr(models, "_latest", datetime.min.replace(tzinfo=UTC))
    monkeypatch.setattr(models, "_wall_clock", lambda: next(readings))
    assert models.now() == first
    assert models.now() == first + timedelta(minutes=1)


def test_artifacts_are_stamped_under_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write that lands before ours stamps the task, and a worker must not predate that
    # stamp: the time put_object is given is read only once the write lock is held.
    database = Database(tmp_path / "data")
    database.migrate()
    locked: list[bool] = []

    def probe() -> datetime:
        other = sqlite3.connect(database.path, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            locked.append(False)
        except sqlite3.OperationalError:
            locked.append(True)
        finally:
            other.close()
        return models.now()

    monkeypatch.setattr(artifacts, "now", probe)
    context = OperationContext("t", "test", "r", models.now(), "r")
    with pytest.raises(artifacts.ArtifactRefused):
        artifacts.store(database, context, {}, {}, "p")
    assert locked == [True]
