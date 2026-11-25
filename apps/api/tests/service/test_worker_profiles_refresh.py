"""The kernel reads the helper's worker profiles again after a while, so a host owner's
change takes effect without a restart, and a failed read keeps the last good copy."""

from typing import Any, cast

import pytest

from cardea.service import confined
from cardea.service.confined import Workers
from cardea.service.database import Fault


class Host:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any] | Fault] = []
        self.calls = 0

    def profiles(self) -> dict[str, Any]:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Fault):
            raise answer
        return answer


def doc(*workers: str) -> dict[str, Any]:
    return {"profiles": {"p": {"wall_seconds": 60}}, "workers": {w: ["p"] for w in workers}}


def test_profiles_are_read_again_after_the_refresh_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("cardea.service.confined.time.monotonic", lambda: clock[0])
    host = Host()
    host.answers = [doc("a"), doc("a", "b")]
    workers = Workers(cast(Any, host))
    assert set(workers.installed()["workers"]) == {"a"}
    clock[0] += confined.PROFILES_REFRESH_SECONDS / 2
    assert set(workers.installed()["workers"]) == {"a"}  # still cached
    assert host.calls == 1
    clock[0] += confined.PROFILES_REFRESH_SECONDS
    assert set(workers.installed()["workers"]) == {"a", "b"}  # the host owner added b
    assert host.calls == 2


def test_a_failed_read_keeps_the_last_good_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("cardea.service.confined.time.monotonic", lambda: clock[0])
    host = Host()
    host.answers = [doc("a"), Fault("host_unavailable", "down", 503)]
    workers = Workers(cast(Any, host))
    assert set(workers.installed()["workers"]) == {"a"}
    clock[0] += confined.PROFILES_REFRESH_SECONDS + 1
    assert set(workers.installed()["workers"]) == {"a"}


def test_no_profiles_before_the_first_good_read() -> None:
    host = Host()
    host.answers = [Fault("host_unavailable", "down", 503)]
    assert Workers(cast(Any, host)).installed() == {"profiles": {}, "workers": {}}
