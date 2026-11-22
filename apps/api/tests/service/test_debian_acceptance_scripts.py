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
import json
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
PROGRAMS = (
    "upgrade.py",
    "long_call.py",
    "groups.py",
    "teams.py",
    "events_tls.py",
    "launcher.py",
    "email_live.py",
    "env_try.py",
    "registry_egress.py",
    "gate_isolation.py",
)


@pytest.fixture
def scripts() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield
    finally:
        sys.path.remove(str(SCRIPTS))
        for name in (
            "common",
            "plan",
            "stack",
            "upgrade",
            "events_tls",
            "teams",
            "long_call",
            "oidc_issuer",
            "launcher",
            "devauth",
            "email_live",
            "coding_fixture",
            "gate_isolation",
        ):
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
        ["registry_egress.py", "--help"],
        ["gate_isolation.py", "--help"],
        ["gate_isolation.py", "run", "--help"],
        ["launcher.py", "--help"],
        ["env_try.py", "--help"],
        ["env_try.py", "run", "--help"],
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
        "coding_fixture.py",
        "common.py",
        "oidc_issuer.py",
        "plan.py",
        "stack.py",
    }


def test_shell_wrappers_are_valid_and_restore_the_owner_configuration() -> None:
    for name in ("long_call.sh", "coding_run.sh"):
        script = SCRIPTS / name
        subprocess.run(["sh", "-n", str(script)], check=True)
        text = script.read_text()
        assert "--no-verify" not in text and "pkill" not in text and "killall" not in text
    text = (SCRIPTS / "coding_run.sh").read_text()
    assert "trap restore EXIT" in text and ".before-acceptance" in text
    assert "coding_run.sh" in (SCRIPTS / "long_call.sh").read_text()


def test_the_test_aliases_are_added_to_the_host_registry_never_over_it(
    tmp_path: Path, scripts: None
) -> None:
    from cardea.service.repositories import Registry

    fixture = load("coding_fixture")
    gates = load("gate_isolation")
    existing = tmp_path / "repositories.json"
    existing.write_text('{"repositories": {"app": {"source": "/opt/cardea-code/repos/app.git"}}}')
    merged = fixture.merged_registry(existing, gates.registry_entries())
    registry = Registry.model_validate(merged)
    assert {"app", *gates.CASES} == set(registry.repositories)
    assert fixture.merged_registry(tmp_path / "absent.json", {"x": {}})["repositories"] == {"x": {}}
    with pytest.raises(SystemExit):
        fixture.merged_registry(existing, {"app": {}})
    # Every case's gates are files the wrapper writes.
    named = {g for case in gates.CASES.values() for g in case}
    assert named <= set(gates.GATES)


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
    from cardea.service.database import SCHEMA_HEAD

    assert upgrade.revision(database) == SCHEMA_HEAD
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


def test_the_long_call_dependencies_follow_the_installed_profile(
    scripts: None, tmp_path: Path
) -> None:
    # A `cache` setting on a profile with registries is refused (dependencies_mismatch).
    long_call = load("long_call")
    workers = tmp_path / "cardea-host-workers.json"
    kit = Path(__file__).resolve().parents[4] / "deploy/small-host/cardea"
    installed = json.loads((kit / "cardea-host-workers.json").read_text())
    workers.write_text(json.dumps(installed))
    assert long_call.coding_config("/r", workers)["dependencies"] == "registries"
    # The strict option: the same profile with the strict network, no registries.
    strict = json.loads((kit / "strict-cache" / "coding-network.json").read_text())
    installed["profiles"]["coding"]["network"] = strict
    workers.write_text(json.dumps(installed))
    assert long_call.coding_config("/r", workers)["dependencies"] == "cache"
    assert long_call.coding_config("/r", tmp_path / "absent.json")["dependencies"] == "cache"


def oidc_config(tmp_path: Path, issuer: object) -> object:
    from cardea.service.models import ServiceConfig

    secret = tmp_path / "client-secret"
    secret.write_text(issuer.client_secret + "\n")  # type: ignore[attr-defined]
    secret.chmod(0o600)
    return ServiceConfig.model_validate(
        {
            "data_dir": str(tmp_path / "store"),
            "spiffe_socket": "unix:///nonexistent",
            "trust_domain": "acceptance.invalid",
            "runtime_spiffe_id": "spiffe://acceptance.invalid/runtime",
            "oidc": {
                "issuer": issuer.url,  # type: ignore[attr-defined]
                "client_id": issuer.client_id,  # type: ignore[attr-defined]
                "client_secret_file": str(secret),
                "allow_loopback_http": True,
            },
        }
    )


def test_the_test_issuer_passes_the_kernels_oidc_validation(tmp_path: Path, scripts: None) -> None:
    """The launcher's identity checks OIDC tokens with the kernel's own discovery and
    introspection code, against the test issuer over loopback."""
    sys.path.insert(0, str(SCRIPTS.parents[0] / "try"))
    try:
        from cardea.service.database import Fault

        issuer_module = load("oidc_issuer")
        launcher = load("launcher")
        (tmp_path / "store").mkdir(mode=0o700)
        with issuer_module.Issuer() as issuer:
            identity = launcher.AcceptanceIdentity(oidc_config(tmp_path, issuer))
            proof = identity.validate(issuer.mint("human-1"), "oidc")
            assert (proof.kind, proof.issuer, proof.subject) == ("oidc", issuer.url, "human-1")
            assert issuer.introspections == 1
            for bad in (
                issuer.mint("human-1", audience="another-service"),
                issuer.mint("human-1", seconds=-30),
                issuer.mint("human-1", scope="profile"),
                issuer.mint("human-1")[:-4] + "AAAA",
            ):
                with pytest.raises(Fault):
                    identity.validate(bad, "oidc")
            # Development SPIFFE tokens still work beside it.
            issue = load("devauth").issue

            spiffe = identity.validate(
                issue(identity.dev.config, "spiffe://acceptance.invalid/sup"), "spiffe"
            )
            assert spiffe.subject == "spiffe://acceptance.invalid/sup"
            identity.close()
    finally:
        sys.path.remove(str(SCRIPTS.parents[0] / "try"))


def test_the_test_issuer_refuses_unauthenticated_introspection(scripts: None) -> None:
    import httpx

    with load("oidc_issuer").Issuer() as issuer:
        token = issuer.mint("human-1")
        refused = httpx.post(f"{issuer.url}/introspect", data={"token": token})
        assert refused.status_code == 401
        answered = httpx.post(
            f"{issuer.url}/introspect",
            data={"token": token},
            auth=(issuer.client_id, issuer.client_secret),
        )
        assert answered.json()["active"] is True and answered.json()["sub"] == "human-1"
        keys = httpx.get(f"{issuer.url}/jwks").json()["keys"]
        assert keys and "d" not in keys[0]


def test_the_env_try_wrapper_restores_the_registry_and_its_catalog_is_valid(
    scripts: None,
) -> None:
    script = SCRIPTS / "env_try.sh"
    subprocess.run(["sh", "-n", str(script)], check=True)
    text = script.read_text()
    assert "trap restore EXIT" in text and "pkill" not in text and "killall" not in text
    env_try = load("env_try")
    registry = Registry.model_validate(env_try.catalog("/opt/cardea-code/repos/x.git"))
    (entry,) = registry.repositories.values()
    assert entry.environment is not None and not entry.hosted
    assert set(entry.environment.scenarios) == {
        "try",
        "teams",
        "slow",
        "loopback",
        "forge",
        "background",
        "check",
        "killer",
    }
    # The scenario is the commit's own code, which the catalog only names.
    assert all(s.argv[2] == "scenario.py" for s in entry.environment.scenarios.values())
    # Only the loopback scenario asks for the private-namespace phase, after its fetch.
    needing = {n for n, s in entry.environment.scenarios.items() if s.network == "loopback"}
    assert needing == {"loopback"}
    assert entry.environment.scenarios["loopback"].fetch is not None
    assert "/proc/net/route" in env_try.SCENARIO
    compile(env_try.SCENARIO, "scenario.py", "exec")
