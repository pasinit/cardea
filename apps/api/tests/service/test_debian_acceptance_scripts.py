"""The Debian acceptance scripts parse, answer --help and keep their own rules (not a run).

The scripts in experiments/debian-acceptance run on a Debian host against the installed
package; these tests only check what can be checked anywhere: that each one parses and
answers `--help` without touching a store, that the working-directory guard refuses what
it must, that the scripted harness plays its plan in order, that the Standard Webhooks
signer matches the kernel's library, and that the upgrade check finds the append-only
triggers of a fresh store.
"""

import ast
import base64
import importlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from standardwebhooks.webhooks import Webhook

from cardea.service.admin import initialize
from cardea.service.repositories import Registry

SCRIPTS = Path(__file__).resolve().parents[4] / "experiments" / "debian-acceptance"
PROGRAMS = ("upgrade.py", "long_call.py", "groups.py", "teams.py", "events_tls.py", "launcher.py")


@pytest.fixture
def scripts() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield
    finally:
        sys.path.remove(str(SCRIPTS))
        for name in ("common", "plan", "stack", "upgrade", "events_tls", "teams", "long_call"):
            sys.modules.pop(name, None)


def load(name: str) -> ModuleType:
    return importlib.import_module(name)


@pytest.mark.parametrize("path", sorted(p.name for p in SCRIPTS.glob("*.py")))
def test_every_script_parses(path: str) -> None:
    ast.parse((SCRIPTS / path).read_text(), filename=path)


@pytest.mark.parametrize(
    "argv",
    [
        ["upgrade.py", "--help"],
        ["long_call.py", "--help"],
        ["long_call.py", "run", "--help"],
        ["groups.py", "--help"],
        ["teams.py", "--help"],
        ["events_tls.py", "--help"],
        ["launcher.py", "--help"],
    ],
)
def test_help_works_without_a_store(argv: list[str], tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / argv[0]), *argv[1:]],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout
    assert not list(tmp_path.iterdir())


def test_every_program_is_covered_by_help() -> None:
    assert {p.name for p in SCRIPTS.glob("*.py")} - set(PROGRAMS) == {
        "common.py",
        "plan.py",
        "stack.py",
    }


def test_shell_wrapper_is_valid_and_restores_the_owner_configuration() -> None:
    script = SCRIPTS / "long_call.sh"
    subprocess.run(["sh", "-n", str(script)], check=True)
    text = script.read_text()
    assert "trap restore_config EXIT" in text and "--no-verify" not in text
    assert "pkill" not in text and "killall" not in text


def test_no_script_signals_processes_by_name() -> None:
    for path in SCRIPTS.glob("*.py"):
        text = path.read_text()
        assert "pkill" not in text and "killall" not in text, path.name


def test_the_work_directory_guard(tmp_path: Path, scripts: None) -> None:
    common = load("common")
    work = common.claim(tmp_path / "w", "one")
    (work / "leftover").write_text("x")
    # The same script reuses and empties its own directory.
    again = common.claim(tmp_path / "w", "one")
    assert again == work and not (work / "leftover").exists()
    # Another script, a directory without the sentinel and a store are refused.
    with pytest.raises(SystemExit):
        common.claim(tmp_path / "w", "two")
    foreign = tmp_path / "store"
    foreign.mkdir()
    (foreign / "kernel.sqlite3").write_text("")
    with pytest.raises(SystemExit):
        common.claim(foreign, "one")
    for path in (Path("/"), Path("/var/lib/cardea"), Path.home()):
        with pytest.raises(SystemExit):
            common.claim(path, "one")


def test_a_live_process_of_an_earlier_run_blocks_a_rerun(tmp_path: Path, scripts: None) -> None:
    common = load("common")
    work = common.claim(tmp_path / "w", "one")
    processes = common.Processes(work)
    child = processes.start(
        [sys.executable, "-c", "import time; time.sleep(60)", str(work)],
        work / "child.log",
        {},
    )
    try:
        with pytest.raises(SystemExit):
            common.claim(tmp_path / "w", "one")
    finally:
        processes.close()
    assert child.poll() is not None
    common.claim(tmp_path / "w", "one")


def test_the_plan_is_played_in_order_then_the_default(tmp_path: Path, scripts: None) -> None:
    plan = load("plan")
    fail = {"calls": [], "outcome": "fail"}
    plan.set_plan(tmp_path, "a1", [fail, {"calls": [{"tool": "x"}]}], {"calls": [], "sleep": 1})
    assert plan.next_action(tmp_path, "a1") == fail
    assert plan.next_action(tmp_path, "a1") == {"calls": [{"tool": "x"}]}
    assert plan.next_action(tmp_path, "a1") == {"calls": [], "sleep": 1}
    assert plan.next_action(tmp_path, "nobody") == plan.SUCCEED
    assert plan.tool_name("records.claim", ("records-claim",)) == "records-claim"
    prompt = '<context operation="mailbox.inbox" status="ok">{"messages": []}</context>'
    assert plan.inbox("before " + prompt) == {"messages": []}
    assert plan.inbox("no inbox") is None


@pytest.mark.parametrize("module", ["events_tls", "teams"])
def test_the_webhook_signer_matches_standard_webhooks(module: str, scripts: None) -> None:
    signer = load(module)
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    body = b'{"specversion":"1.0"}'
    headers = {
        "webhook-id": "msg-1",
        "webhook-timestamp": str(int(__import__("time").time())),
    }
    headers["webhook-signature"] = signer.sign(
        secret, "msg-1", int(headers["webhook-timestamp"]), body
    )
    Webhook(secret).verify(body, headers)


def test_the_upgrade_check_finds_append_only_triggers(tmp_path: Path, scripts: None) -> None:
    upgrade = load("upgrade")
    initialize(tmp_path / "store")
    database = tmp_path / "store" / "kernel.sqlite3"
    found = {g["trigger"]: g for g in upgrade.guards(database)}
    assert {"record_events_no_update", "record_events_no_delete", "events_no_delete"} <= set(found)
    assert found["record_events_no_delete"]["action"] == "DELETE"
    # A fresh store has no rows to change: the probe reports that rather than passing.
    assert upgrade.probe(database, found["record_events_no_delete"]) == "empty"
    assert upgrade.revision(database) == "m17_content_redaction"
    assert upgrade.schema(database) == upgrade.schema(database)


def test_the_long_call_configuration_names_only_the_fixture(scripts: None) -> None:
    long_call = load("long_call")
    config = long_call.coding_config("/opt/cardea-code/repos/acceptance.git")
    assert "repositories" not in config  # they live in the host registry (ADR-037)
    registry = Registry.model_validate(
        long_call.repositories("/opt/cardea-code/repos/acceptance.git")
    )
    (alias,) = registry.repositories
    assert alias == long_call.REPOSITORY_ALIAS and not registry.repositories[alias].hosted
    assert config["harness"]["argv"][:2] == ["/usr/bin/python3", "-I"]
    assert "CARDEA_TASK" in config["harness"]["argv"][3]
