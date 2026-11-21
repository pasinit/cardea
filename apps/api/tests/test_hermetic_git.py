"""Git in the test suite ignores the developer's configuration and hooks."""

import os
import subprocess
from pathlib import Path


def test_git_ignores_global_config_and_hooks(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hook = tmp_path / ".git" / "hooks" / "commit-msg"
    hook.write_text("#!/bin/sh\nexit 1\n")  # would refuse every commit if it ran
    hook.chmod(0o755)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "test"], check=True)
    shown = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--show-origin", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "core.hookspath=" in shown and "file:" + str(Path.home()) not in shown
    author = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "-1", "--format=%ae"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert author == "tests@example.invalid"
