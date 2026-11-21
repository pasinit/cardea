"""Suite-wide test settings.

Git runs hermetically: tests create scratch repositories and commit to them, and must not
pick up the developer's global or system configuration (identity, signing, hooks). A global
`core.hooksPath` hook once failed an unrelated test.
"""

import os
import tempfile

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
