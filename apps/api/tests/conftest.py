"""Suite-wide test settings and fixtures.

Git runs hermetically: tests create scratch repositories and commit to them, and must not
pick up the developer's global or system configuration (identity, signing, hooks). A global
`core.hooksPath` hook once failed an unrelated test.
"""

import logging
import os
import socket
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# Fictional identity for test commits; nothing here reaches a real repository.
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Cardea Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "Cardea Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}


def pytest_configure(config: pytest.Config) -> None:
    hooks = tempfile.mkdtemp(prefix="cardea-test-git-hooks-")
    os.environ.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            # Applies to every repository, overriding repository-local settings too.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": hooks,
            **GIT_IDENTITY,
        }
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests listed in `loopback_tests.txt` as `loopback`: they run an agent run or
    operation whose tool endpoint or content gate binds TCP loopback (ADR-027). One list
    instead of a decorator in every file; a test that needs TCP and is on neither fails by
    name under `make check-test`."""
    path = Path(__file__).parent / "loopback_tests.txt"
    listed = set(path.read_text().split()) if path.exists() else set()
    for item in items:
        if item.nodeid.removeprefix("tests/").split("[")[0] in listed:
            item.add_marker(pytest.mark.loopback)


@pytest.fixture(autouse=True)
def no_tcp_unless_marked(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Under `make check-test` (CARDEA_NO_LOOPBACK=1) a test that is not marked `loopback`
    may not use TCP: it fails by name instead of hanging in a sandbox without loopback, so
    the `check` scenario's exclusion cannot silently drift."""
    if os.environ.get("CARDEA_NO_LOOPBACK") != "1" or request.node.get_closest_marker("loopback"):
        yield
        return

    def refuse(original: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(self: socket.socket, address: Any, *args: Any) -> Any:
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise RuntimeError(f"{request.node.nodeid} uses TCP; mark it `loopback`")
            return original(self, address, *args)

        return guarded

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, refuse(getattr(socket.socket, name)))
    yield


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture every `cardea.*` record, even after the service configured its handler."""
    root = logging.getLogger("cardea")
    propagate, handlers = root.propagate, root.handlers[:]
    root.propagate, root.handlers[:] = True, []
    caplog.set_level(logging.DEBUG, logger="cardea")
    try:
        yield caplog
    finally:
        root.propagate, root.handlers[:] = propagate, handlers
