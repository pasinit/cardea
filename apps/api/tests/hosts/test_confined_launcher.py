"""Root helper side of confined worker profiles (ADR-028); portable, no host commands run."""

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_m2_completion import helper as helper

from cardea.hosts import launcher, operation_worker
from cardea.service.operations import OperationContext, OperationRejected

BROWSER = {
    "memory_mb": 512,
    "cpu_percent": 100,
    "tasks": 64,
    "wall_seconds": 120,
    "scratch_mb": 256,
    "output_kb": 256,
    "network": {"policy": "allow_list", "proxy": "127.0.10.1:3128", "hosts": ["example.org"]},
    "jit": True,
}
SESSION = {
    "memory_mb": 768,
    "cpu_percent": 100,
    "tasks": 128,
    "wall_seconds": 900,
    "scratch_mb": 128,
    "output_kb": 64,
    "network": {
        "policy": "model_api_only",
        "proxy": "127.0.10.2:3128",
        "hosts": ["api.anthropic.com"],
    },
    "jit": True,
    "credential": "/etc/cardea/credentials/claude",
}
SMALL = {
    "memory_mb": 64,
    "cpu_percent": 50,
    "tasks": 8,
    "wall_seconds": 8,
    "scratch_mb": 1,
    "output_kb": 64,
    "network": {"policy": "none"},
}
CONFIG = {
    "profiles": {"browser": BROWSER, "session": SESSION, "small": SMALL},
    "workers": {
        "fetch": {
            "argv": ["/opt/cardea/bin/python3", "-I", "-m", "cardea.hosts.operation_worker", "f"],
            "profiles": ["browser"],
            "read_only": ["/opt/cardea"],
            "env": {"FETCH_MODE": "text"},
        },
        "agent": {
            "argv": ["/opt/cardea/bin/python3", "-I", "-m", "cardea.hosts.session_worker"],
            "profiles": ["session"],
            "read_only": ["/opt/cardea"],
            "env": {"CARDEA_CREDENTIAL_KIND": "oauth_token"},
        },
        "hash": {"argv": ["/usr/bin/sha256sum"], "profiles": ["small"]},
    },
}


@pytest.fixture
def configured(helper: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The helper with CONFIG installed; returns the command lines it would run."""
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher, "worker_config", lambda: CONFIG)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(
            args, 0, "ActiveState=inactive\nLoadState=not-found\n", ""
        )

    monkeypatch.setattr(launcher, "run", run)
    return commands


def start(job: str, profile: str, worker: str, **changes: Any) -> dict[str, Any]:
    return {
        "action": "start_confined",
        "job": job,
        "profile": profile,
        "worker": worker,
        "deadline": time.time() + 30,
        "input": base64.b64encode(b'{"arguments":{}}').decode(),
        **changes,
    }


def properties(command: list[str]) -> dict[str, str]:
    return dict(
        arg.removeprefix("--property=").split("=", 1)
        for arg in command
        if arg.startswith("--property=")
    )


def environment(command: list[str]) -> dict[str, str]:
    return {command[i + 1]: command[i + 2] for i, arg in enumerate(command) if arg == "--setenv"}


def test_allow_list_profile_limits_and_network_reach_systemd_and_bubblewrap(
    configured: list[list[str]], helper: Path
) -> None:
    job = "b" * 32
    assert launcher.handle(start(job, "browser", "fetch")) == {"started": True}
    (command,) = configured
    props = properties(command)
    assert props["MemoryMax"] == "512M" and props["MemorySwapMax"] == "0"
    assert props["TasksMax"] == "64" and props["CPUQuota"] == "100%"
    assert 29 < float(props["RuntimeMaxSec"].removesuffix("s")) <= 30
    assert props["KillMode"] == "control-group" and props["OOMPolicy"] == "kill"
    assert props["StandardInput"] == f"file:{helper / job}/input/data"
    assert props["StandardOutput"] == f"file:{helper / job}/output"
    # Egress only to the proxy; no private network namespace, no tool-endpoint loopback.
    assert "PrivateNetwork" not in props
    assert props["IPAddressDeny"] == "any" and props["IPAddressAllow"] == "127.0.10.1/32"
    assert props["RestrictAddressFamilies"] == "AF_UNIX AF_INET AF_INET6"
    assert props["MemoryDenyWriteExecute"] == "no"
    assert props["User"] == "cardea-worker" and props["NoNewPrivileges"] == "yes"
    env = environment(command)
    assert env["HTTPS_PROXY"] == "http://127.0.10.1:3128" and env["FETCH_MODE"] == "text"
    assert "CARDEA_CREDENTIAL" not in env and "LoadCredential" not in props
    bwrap = command[command.index("/usr/bin/bwrap") :]
    assert bwrap[bwrap.index("--size") + 1] == str(256 * 1048576)
    assert bwrap[bwrap.index("/opt/cardea") - 1 : bwrap.index("/opt/cardea") + 2] == [
        "--ro-bind",
        "/opt/cardea",
        "/opt/cardea",
    ]
    assert bwrap[-5:] == CONFIG["workers"]["fetch"]["argv"]  # type: ignore[index]
    assert json.loads((helper / job / "input/data").read_bytes()) == {"arguments": {}}


def test_session_profile_gets_one_credential_and_the_tool_endpoint(
    configured: list[list[str]],
) -> None:
    job = "c" * 32
    launcher.handle(start(job, "session", "agent"))
    props = properties(configured[0])
    assert props["IPAddressAllow"] == "127.0.10.2/32 127.0.0.1/32"
    assert props["LoadCredential"] == "cardea:/etc/cardea/credentials/claude"
    command = configured[0]
    source = f"/run/credentials/cardea-host-{job}.service/cardea"
    assert command[command.index(source) + 1] == launcher.CREDENTIAL
    env = environment(command)
    assert env["CARDEA_CREDENTIAL"] == launcher.CREDENTIAL
    assert env["NO_PROXY"] == "127.0.0.1,localhost"


def test_none_profile_keeps_the_m2_network_denial(configured: list[list[str]]) -> None:
    launcher.handle(start("d" * 32, "small", "hash"))
    props = properties(configured[0])
    assert props["PrivateNetwork"] == "yes" and props["RestrictAddressFamilies"] == "AF_UNIX"
    assert "@network-io" in props["SystemCallFilter"]
    assert props["MemoryMax"] == "64M" and props["MemoryDenyWriteExecute"] == "yes"
    assert "IPAddressAllow" not in props


def test_wall_time_is_capped_by_the_profile(configured: list[list[str]]) -> None:
    launcher.handle(start("e" * 32, "small", "hash", deadline=time.time() + 600))
    assert properties(configured[0])["RuntimeMaxSec"] == "8.000s"


@pytest.mark.parametrize(
    ("profile", "worker", "reason"),
    [
        ("browser", "missing", "profile_not_installed"),
        ("missing", "fetch", "profile_not_installed"),
        ("small", "fetch", "profile_not_installed"),  # worker not allowed under profile
        ("Browser", "fetch", "profile_not_installed"),
    ],
)
def test_unknown_or_unpermitted_profiles_are_refused_before_the_fence(
    configured: list[list[str]], helper: Path, profile: str, worker: str, reason: str
) -> None:
    job = "f" * 32
    with pytest.raises(launcher.Rejected, match=f"^{reason}$"):
        launcher.handle(start(job, profile, worker))
    assert configured == [] and not (helper / job).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"argv": ["/bin/sh"]},
        {"extra": True},
        {"command": "/opt/x"},
        {"profile_override": {"memory_mb": 99999}},
    ],
)
def test_requests_cannot_carry_command_lines_or_limits(
    configured: list[list[str]], change: dict[str, Any]
) -> None:
    with pytest.raises(launcher.Rejected, match="^invalid_request$"):
        launcher.handle(start("a" * 32, "browser", "fetch") | change)
    assert configured == []


@pytest.mark.parametrize(
    ("section", "name", "change"),
    [
        ("profiles", "browser", {"memory_mb": 0}),
        ("profiles", "browser", {"memory_mb": "512"}),
        ("profiles", "browser", {"network": {"policy": "open"}}),
        ("profiles", "browser", {"network": {"policy": "none", "hosts": ["a.example"]}}),
        (
            "profiles",
            "browser",
            {"network": {"policy": "allow_list", "proxy": "127.0.0.1:3128", "hosts": ["a.b"]}},
        ),
        (
            "profiles",
            "browser",
            {"network": {"policy": "allow_list", "proxy": "10.0.0.1:3128", "hosts": ["a.b"]}},
        ),
        ("profiles", "browser", {"credential": "/var/lib/cardea/secret"}),
        ("profiles", "browser", {"unknown": 1}),
        ("workers", "fetch", {"argv": ["python3"]}),
        ("workers", "fetch", {"argv": ["/home/user/run"]}),
        ("workers", "fetch", {"read_only": ["/var/lib/cardea"]}),
        ("workers", "fetch", {"read_only": ["/opt/../var/lib/cardea"]}),
        ("workers", "fetch", {"read_only": ["/etc"]}),
        ("workers", "fetch", {"env": {"HTTPS_PROXY": "http://evil"}}),
        ("workers", "fetch", {"env": {"PATH": "/tmp"}}),
        ("workers", "fetch", {"profiles": ["missing"]}),
    ],
)
def test_invalid_host_owner_configuration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, name: str, change: dict[str, Any]
) -> None:
    config = json.loads(json.dumps(CONFIG))
    config[section][name] |= change
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(launcher, "WORKERS_CONFIG", path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    real = Path.lstat

    def owned_by_root(self: Path) -> os.stat_result:
        info = real(self)
        if self == path:
            return os.stat_result((info.st_mode & ~0o022, *info[1:4], 0, *info[5:]))
        return info

    monkeypatch.setattr(Path, "lstat", owned_by_root)
    with pytest.raises(ValueError):
        launcher.worker_config()


def test_configuration_must_be_root_owned_and_absent_means_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "workers.json"
    monkeypatch.setattr(launcher, "WORKERS_CONFIG", path)
    assert launcher.worker_config() == {"profiles": {}, "workers": {}}
    path.write_text(json.dumps(CONFIG))
    with pytest.raises(ValueError, match="unsafe_configuration"):
        launcher.worker_config()


def test_profiles_action_reports_limits_but_never_credential_paths(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "worker_config", lambda: CONFIG)
    reported = launcher.handle({"action": "profiles"})
    assert reported["workers"] == {"fetch": ["browser"], "agent": ["session"], "hash": ["small"]}
    session = reported["profiles"]["session"]
    assert session["credential"] is True and session["memory_mb"] == 768
    assert "/etc/cardea" not in json.dumps(reported)
    assert session["sha256"] == launcher.digest(SESSION)
    with pytest.raises(launcher.Rejected):
        launcher.handle({"action": "profiles", "job": "a" * 32})


def test_fixed_m2_profile_is_unchanged(configured: list[list[str]], helper: Path) -> None:
    launcher.handle(
        {
            "action": "start",
            "job": "a" * 32,
            "operation": "host.sha256",
            "deadline": time.time() + 30,
            "input": "e30=",
        }
    )
    props = properties(configured[0])
    assert props["MemoryMax"] == "64M" and props["TasksMax"] == "8"
    assert props["CPUQuota"] == "50%" and props["LimitFSIZE"] == "64K"
    assert props["PrivateNetwork"] == "yes" and props["StandardInput"] == "null"
    assert float(props["RuntimeMaxSec"].removesuffix("s")) <= 8


# The in-sandbox operation worker


class Point:
    name = "pages.fetch"

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    def load(self) -> Any:
        return self.handler


def run_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], handler: Any
) -> dict[str, Any]:
    monkeypatch.setattr(operation_worker, "entry_points", lambda group: [Point(handler)])
    document = {
        "arguments": {"url": "https://example.org"},
        "context": {
            "task_id": "task-1",
            "namespace": "test",
            "receipt_id": "receipt-1",
            "deadline": "2030-01-01T00:00:00+00:00",
            "effect_key": "key-1",
        },
    }
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(document).encode()))
    monkeypatch.setattr(sys, "stdin", stdin)
    assert operation_worker.main(["operation_worker", "pages.fetch"]) == 0
    return dict(json.loads(capsys.readouterr().out))


def test_operation_worker_speaks_json_on_stdin_and_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fetch(arguments: dict[str, Any], context: OperationContext) -> dict[str, Any]:
        return {"text": f"{arguments['url']} {context.effect_key}"}

    assert run_worker(monkeypatch, capsys, fetch) == {
        "output": {"text": "https://example.org key-1"}
    }

    async def refuse(arguments: dict[str, Any], context: OperationContext) -> dict[str, Any]:
        raise OperationRejected("blocked by robots.txt")

    assert run_worker(monkeypatch, capsys, refuse) == {"rejected": "blocked by robots.txt"}


def test_debian_probe_configuration_is_a_valid_host_owner_file() -> None:
    from test_confined_debian import config

    probes = config()
    for entry in probes["profiles"].values():
        launcher.validate_profile(entry)
    for entry in probes["workers"].values():
        launcher.validate_worker(entry, probes["profiles"])
