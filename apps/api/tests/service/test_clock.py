"""The service clock never runs backwards within a process."""

from datetime import UTC, datetime, timedelta

import pytest

from cardea.service import models


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
