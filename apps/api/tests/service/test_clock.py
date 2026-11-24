from datetime import UTC, datetime, timedelta

import pytest

from cardea.service import models


def test_now_never_steps_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 11, 24, 12, tzinfo=UTC)
    readings = iter([start, start - timedelta(seconds=5), start + timedelta(seconds=1)])
    monkeypatch.setattr(models, "_latest", datetime.min.replace(tzinfo=UTC))
    monkeypatch.setattr(models, "_wall_clock", lambda: next(readings))
    assert models.now() == start
    assert models.now() == start
    assert models.now() == start + timedelta(seconds=1)
