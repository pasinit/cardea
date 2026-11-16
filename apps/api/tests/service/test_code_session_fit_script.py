"""The code.session Debian fit script stays consistent with the worker (not a run of it)."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cardea_coding_ops import claude_code
from cardea_coding_ops.session import fixed_env

from cardea.service.repositories import Registry

FIT = Path(__file__).resolve().parents[4] / "experiments" / "code-session"


def fit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("code_session_fit", FIT / "fit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_is_a_repository_that_tries_to_run_its_own_configuration(
    tmp_path: Path,
) -> None:
    module = fit()
    module.make_fixture(tmp_path / "fixture.git")
    listed = subprocess.run(
        ["git", "-C", str(tmp_path / "fixture.git"), "ls-tree", "-r", "--name-only", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert {".claude/settings.json", ".mcp.json", "calc.py", "test_calc.py"} <= set(listed)
    settings = json.loads(module.FIXTURE[".claude/settings.json"])
    assert "hook-ran" in json.dumps(settings["hooks"]) and "hook-ran" in module.MARKERS


def test_fit_configuration_and_payload_match_the_worker() -> None:
    module = fit()
    config = module.coding_config()
    fixed_env(config["gate_env"])
    harness = config["harness"]
    command = claude_code.argv({**harness, "binary": "/opt/x/claude"})
    assert "--bare" not in command and harness["credential_kind"] == "oauth_token"
    payload = module.payload("0" * 32)
    assert set(payload) == {"arguments", "context"}
    assert set(payload["arguments"]) == {"repository", "base_ref", "task", "task_key"}
    assert "repositories" not in config
    registry = Registry.model_validate(module.repositories())
    assert payload["arguments"]["repository"] in registry.repositories
    assert (module.PROFILE, module.WORKER) == ("coding", "code-session")


def test_debian_script_is_valid_shell_and_never_prints_the_token() -> None:
    script = FIT / "debian.sh"
    subprocess.run(["sh", "-n", str(script)], check=True)
    text = script.read_text()
    assert "0600" in text and 'cat "$TOKEN_FILE"' not in text and "--no-verify" not in text


# The helper's `profiles` answer, in the shape `cardea.service.confined.Workers` reads.
INSTALLED: dict[str, Any] = {
    "profiles": {
        "coding": {"wall_seconds": 3600, "network": {"policy": "model_api_only"}, "sha256": "p"},
        "session": {"wall_seconds": 1800, "network": {"policy": "model_api_only"}},
    },
    "workers": {"code-session": ["coding"], "agent": ["session"]},
}


class FakeHost:
    """The helper's interface, answering `profiles` with the real nested shape."""

    def __init__(self, installed: dict[str, Any]) -> None:
        self.installed = installed

    def profiles(self) -> dict[str, Any]:
        return self.installed


def test_the_fit_reads_the_coding_profile_from_the_nested_answer() -> None:
    from cardea.service.confined import Workers

    host = FakeHost(INSTALLED)
    profile = fit().installed_profile(host.profiles())
    assert profile["network"]["policy"] == "model_api_only" and profile["wall_seconds"] == 3600
    # The answer the kernel itself reads, so the fit test checks what the kernel will run.
    assert Workers(host).installed()["profiles"]["coding"] == profile  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "installed",
    [
        # The old reading: profiles at the top level are not the helper's answer.
        INSTALLED["profiles"],
        {"profiles": {"session": INSTALLED["profiles"]["session"]}, "workers": {}},
        {"profiles": INSTALLED["profiles"], "workers": {"code-session": []}},
        {
            "profiles": {"coding": {"network": {"policy": "model_api_only"}}},
            "workers": {"code-session": ["coding"]},
        },
    ],
)
def test_a_missing_or_incomplete_profile_stops_the_fit_loudly(installed: dict[str, Any]) -> None:
    with pytest.raises(SystemExit):
        fit().installed_profile(installed)
