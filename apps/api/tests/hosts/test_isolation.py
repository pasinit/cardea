"""Untrusted steps in a confined worker leave nothing behind (cardea.hosts.isolation)."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import cardea
from cardea.hosts import isolation

LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="needs /proc, PR_SET_DUMPABLE and PR_SET_CHILD_SUBREAPER (Linux)",
)
ENV = {"PATH": "/usr/bin:/bin"}


def python(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def test_a_step_reports_its_output_and_exit_status(tmp_path: Path) -> None:
    done = isolation.run_step(
        python("import sys; print(sys.stdin.read().upper()); sys.exit(3)"),
        tmp_path,
        ENV,
        10,
        stdin="hello",
    )
    assert (done.returncode, done.stdout.strip(), done.timed_out, done.killed) == (
        3,
        "HELLO",
        False,
        0,
    )


def test_a_background_process_in_the_steps_group_is_killed(tmp_path: Path) -> None:
    done = isolation.run_step(["sh", "-c", "sleep 30 & echo started"], tmp_path, ENV, 10)
    assert done.stdout.strip() == "started" and done.killed >= 1
    assert isolation.descendants() == []


@LINUX
def test_a_detached_grandchild_is_found_and_killed(tmp_path: Path) -> None:
    """`setsid` and a double fork leave the step's group and session; the subreaper keeps
    the orphan below the worker, so it is still found. Run in a child that is hardened."""
    script = f"""
        import sys
        sys.path.insert(0, {str(Path(cardea.__file__).parents[1])!r})
        from pathlib import Path
        from cardea.hosts import isolation
        print(isolation.harden())
        detach = (
            "import os, time\\n"
            "if os.fork() == 0:\\n"
            "    os.setsid()\\n"
            "    if os.fork() == 0:\\n"
            "        time.sleep(30)\\n"
            "    os._exit(0)\\n"
            "os.wait()\\n"
        )
        done = isolation.run_step([sys.executable, "-c", detach], Path("."), {{}}, 10)
        print(done.killed, isolation.descendants())
    """
    out = subprocess.run(python(script), cwd=tmp_path, capture_output=True, text=True, check=True)
    mode, line = out.stdout.splitlines()
    assert mode == "subreaper" and line == "1 []", out.stdout + out.stderr


def test_a_step_that_overruns_is_killed_with_its_descendants(tmp_path: Path) -> None:
    started = time.monotonic()
    done = isolation.run_step(["sh", "-c", "sleep 30 & sleep 30"], tmp_path, ENV, 0.5)
    assert done.timed_out and done.returncode is None
    assert time.monotonic() - started < 10
    assert isolation.descendants() == []


def test_a_survivor_raises_with_the_callers_code(monkeypatch: pytest.MonkeyPatch) -> None:
    keep = frozenset(isolation.descendants())
    child = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setattr(isolation, "_kill", lambda pid: None)  # an unkillable stray
        monkeypatch.setattr(isolation, "ROUNDS", 3)
        monkeypatch.setattr(isolation, "PAUSE", 0.01)
        with pytest.raises(isolation.IsolationFailed) as failed:
            isolation.clear(keep, code="env_isolation_failed")
        assert failed.value.code == "env_isolation_failed"
    finally:
        child.kill()
        child.wait()


@LINUX
def test_hardening_is_read_back() -> None:
    script = f"""
        import sys
        sys.path.insert(0, {str(Path(cardea.__file__).parents[1])!r})
        from cardea.hosts import isolation
        print(isolation.harden())
        print(isolation._prctl(isolation.PR_GET_DUMPABLE))
    """
    out = subprocess.run(python(script), capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["subreaper", "0"], out.stderr


def test_off_linux_hardening_says_what_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isolation.harden() == "process_group"
