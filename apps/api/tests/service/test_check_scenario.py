"""The `check` scenario of `env.try` (ADR-038): lint, typecheck and the unit tests that need
no TCP loopback, with a log tail that names the failing test ids and the exit code.

Portable: the runner is exercised with stand-in commands, plus one nested pytest run on a
seeded failing test. The real `make lint` and `make typecheck` are not run here.
"""

import importlib.util
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from cardea.service.repositories import Registry

ROOT = Path(__file__).resolve().parents[4]
TESTS = ROOT / "apps" / "api" / "tests"
# env.try keeps the end of the output; the summary must fit in it.
TAIL = 4096


def runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_runner", ROOT / "packaging" / "check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def test_the_catalog_names_check_as_a_process_scenario_running_make_check() -> None:
    registry = Registry.model_validate_json(
        (ROOT / "deploy/small-host/cardea/repositories.json").read_text()
    )
    environment = next(e.environment for e in registry.repositories.values() if e.environment)
    assert environment is not None
    check = environment.scenarios["check"]
    assert (check.argv, check.tier, check.artifacts) == (("/usr/bin/make", "check"), "process", ())
    # It stands for the three scenarios it combines, so it gets their time.
    steps = [environment.scenarios[n].timeout_seconds for n in ("lint", "typecheck", "api-test")]
    assert check.timeout_seconds >= sum(steps)


def test_make_check_runs_the_runner_and_the_tests_without_loopback() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "check:\n\tpython3 packaging/check.py\n" in makefile
    (recipe,) = (line for line in makefile.splitlines() if '-m "not loopback"' in line)
    assert "CARDEA_NO_LOOPBACK=1" in recipe and "-rfE" in recipe and "--tb=short" in recipe
    runner_steps = {name: argv for name, argv in runner().STEPS}
    assert runner_steps == {
        "lint": ("make", "lint"),
        "typecheck": ("make", "typecheck"),
        "test": ("make", "check-test"),
    }
    config = tomllib.loads((ROOT / "apps/api/pyproject.toml").read_text())
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("loopback:") for m in markers)


def test_the_loopback_tests_are_marked() -> None:
    for name in ("test_loopback", "test_network", "test_opencode", "test_confined_harnesses"):
        assert "pytestmark = pytest.mark.loopback" in (TESTS / "service" / f"{name}.py").read_text()


def test_a_passing_run_reports_every_step_and_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    module = runner()
    steps = [("lint", python("print('ok')")), ("test", python("print('1 passed')"))]
    assert module.main(steps) == 0
    out = capsys.readouterr().out
    assert "lint: pass (exit 0)" in out and "test: pass (exit 0)" in out
    assert out.endswith("check: PASS (exit 0)\n")


def test_a_failing_run_names_the_failing_ids_and_the_exit_code_in_the_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = runner()
    noise = "x" * 100
    failing = (
        f"for _ in range(200): print({noise!r})\n"
        "print('FAILED tests/a.py::test_one - assert 1 == 2')\n"
        "print('FAILED tests/b.py::test_two[a b] - boom')\n"
        "print('ERROR tests/c.py::test_three - fixture')\n"
        "print('FAILED tests/a.py::test_one - again')\n"
        "raise SystemExit(1)"
    )
    steps = [("lint", python("raise SystemExit(2)")), ("test", python(failing))]
    assert module.main(steps) == 2  # the first failing step's status
    tail = capsys.readouterr().out[-TAIL:]
    assert "lint: FAIL (exit 2)" in tail and "test: FAIL (exit 1)" in tail
    ids = [line.strip() for line in tail.split("failing tests:\n")[1].splitlines()[:3]]
    assert ids == ["tests/a.py::test_one", "tests/b.py::test_two[a b]", "tests/c.py::test_three"]
    assert tail.endswith("check: FAIL (exit 2)\n")


def test_many_failures_are_capped_so_the_summary_fits_the_tail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = runner()
    lines = "".join(f"FAILED tests/t.py::test_{n:03d} - x\\n" for n in range(60))
    assert module.main([("test", python(f"print('{lines}', end='')\nraise SystemExit(1)"))]) == 1
    out = capsys.readouterr().out
    summary = out[out.rindex("== check summary ==") :]
    assert len(summary) <= TAIL
    assert "tests/t.py::test_019" in summary and "tests/t.py::test_020" not in summary
    assert "... and 40 more" in summary and summary.endswith("check: FAIL (exit 1)\n")


def test_a_step_that_cannot_start_fails_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner().main([("lint", ("/nonexistent/make", "lint"))]) == 127
    assert "check: FAIL (exit 127)" in capsys.readouterr().out


def test_a_commit_with_a_seeded_failing_test_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "test_seeded.py").write_text(
        "def test_good() -> None:\n    pass\n\n\ndef test_seeded_failure() -> None:\n"
        "    assert 1 == 2\n"
    )
    pytest_argv = (
        sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider",
        "--rootdir", str(tmp_path), "-c", str(tmp_path / "pytest.ini"), str(tmp_path),
    )  # fmt: skip
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert runner().main([("lint", python("pass")), ("test", pytest_argv)]) == 1
    tail = capsys.readouterr().out[-TAIL:]
    assert "test_seeded.py::test_seeded_failure" in tail.split("failing tests:\n")[1]
    assert "test_good" not in tail.split("failing tests:\n")[1]
    assert tail.endswith("check: FAIL (exit 1)\n")


def nested(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """A pytest run with this suite's conftest, under `CARDEA_NO_LOOPBACK=1`."""
    shutil.copy(TESTS / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    loopback: tcp\n")
    (tmp_path / "test_tcp.py").write_text(source)
    env = {"CARDEA_NO_LOOPBACK": "1", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


CONNECT = (
    "import socket\n"
    "import pytest\n\n\n"
    "{mark}def test_tcp() -> None:\n"
    "    with socket.socket() as s:\n"
    "        s.settimeout(2)  # a sandbox that drops loopback packets must not hang the connect\n"
    "        try:\n"
    "            s.connect(('127.0.0.1', 9))\n"
    "        except OSError:  # refused, timed out, or no loopback; the guard raises RuntimeError\n"
    "            pass\n"
)


def test_an_unmarked_test_that_uses_tcp_fails_by_name_under_check(tmp_path: Path) -> None:
    done = nested(tmp_path, CONNECT.format(mark=""))
    assert done.returncode == 1
    assert "test_tcp.py::test_tcp uses TCP; mark it `loopback`" in done.stdout


def test_a_test_marked_loopback_may_use_tcp_under_check(tmp_path: Path) -> None:
    done = nested(tmp_path, CONNECT.format(mark="@pytest.mark.loopback\n"))
    assert done.returncode == 0, done.stdout
