"""Root helper side of confined worker profiles (ADR-028); portable, no host commands run."""

import base64
import hashlib
import io
import json
import os
import platform
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cardea_env_ops import try_
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


def test_public_web_profile_reaches_only_its_proxy(
    configured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    web = {**BROWSER, "network": {"policy": "public_web", "proxy": "127.0.10.3:3128"}}
    launcher.validate_profile(web)
    config: dict[str, Any] = json.loads(json.dumps(CONFIG))
    config["profiles"]["web"] = web
    config["workers"]["fetch"]["profiles"] = ["web"]
    monkeypatch.setattr(launcher, "worker_config", lambda: config)
    assert launcher.handle(start("e" * 32, "web", "fetch")) == {"started": True}
    (command,) = configured
    props = properties(command)
    # The same systemd IP filter as allow_list: the proxy decides which public hosts.
    assert props["IPAddressDeny"] == "any" and props["IPAddressAllow"] == "127.0.10.3/32"
    assert "PrivateNetwork" not in props
    assert environment(command)["HTTPS_PROXY"] == "http://127.0.10.3:3128"
    reported = launcher.handle({"action": "profiles"})["profiles"]["web"]
    assert reported["network"] == {"policy": "public_web", "proxy": "127.0.10.3:3128"}


def test_session_profile_gets_one_credential_and_the_tool_endpoint(
    configured: list[list[str]],
) -> None:
    job = "c" * 32
    launcher.handle(start(job, "session", "agent"))
    props = properties(configured[0])
    assert props["IPAddressAllow"] == "127.0.10.2/32 127.0.11.1/32"
    assert props["LoadCredential"] == "cardea:/etc/cardea/credentials/claude"
    command = configured[0]
    source = f"/run/credentials/cardea-host-{job}.service/cardea"
    assert command[command.index(source) + 1] == launcher.CREDENTIAL
    env = environment(command)
    assert env["CARDEA_CREDENTIAL"] == launcher.CREDENTIAL
    assert env["NO_PROXY"] == "127.0.11.1,localhost"


def test_a_profile_may_load_several_named_credentials(
    configured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-037 amendment (2026-11-17): one LoadCredential per name, the unit's credential
    directory bound read-only; the kernel learns the names, never the paths."""
    named = {
        **BROWSER,
        "credentials": {
            "github-author": "/etc/cardea/credentials/github-author",
            "github-reviewer": "/etc/cardea/credentials/github-reviewer",
        },
    }
    launcher.validate_profile(named)
    config: dict[str, Any] = json.loads(json.dumps(CONFIG))
    config["profiles"]["vcs"] = named
    config["workers"]["fetch"]["profiles"] = ["vcs"]
    monkeypatch.setattr(launcher, "worker_config", lambda: config)
    job = "f" * 32
    assert launcher.handle(start(job, "vcs", "fetch")) == {"started": True}
    (command,) = configured
    loads = [a for a in command if a.startswith("--property=LoadCredential=")]
    assert loads == [
        "--property=LoadCredential=github-author:/etc/cardea/credentials/github-author",
        "--property=LoadCredential=github-reviewer:/etc/cardea/credentials/github-reviewer",
    ]
    source = f"/run/credentials/cardea-host-{job}.service"
    assert command[command.index(source) + 1] == launcher.CREDENTIALS
    env = environment(command)
    assert env["CARDEA_CREDENTIALS"] == launcher.CREDENTIALS and "CARDEA_CREDENTIAL" not in env
    reported = launcher.handle({"action": "profiles"})["profiles"]["vcs"]
    assert reported["credentials"] == ["github-author", "github-reviewer"]
    assert "/etc/cardea" not in json.dumps(reported)
    bad: dict[str, Any]
    for bad in (
        {"credentials": {}},
        {"credentials": {"Bad": "/etc/cardea/credentials/x"}},
        {"credentials": {"a": "/tmp/x"}},
        {"credentials": {"a": "/etc/cardea/credentials/x", "b": "/etc/cardea/credentials/x"}},
        {
            "credentials": {"a": "/etc/cardea/credentials/x"},
            "credential": "/etc/cardea/credentials/y",
        },
    ):
        with pytest.raises(ValueError):
            launcher.validate_profile({**BROWSER, **bad})


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
        ("profiles", "browser", {"concurrency": 0}),
        ("profiles", "browser", {"queue_timeout_seconds": 0}),
        ("profiles", "browser", {"queue_timeout_seconds": 3601}),
        ("profiles", "browser", {"queue_timeout_seconds": 1.5}),
        (
            "profiles",
            "browser",
            {"network": {"policy": "public_web", "proxy": "127.0.10.3:3128", "hosts": ["a.b"]}},
        ),
        ("profiles", "browser", {"network": {"policy": "public_web"}}),
        ("profiles", "browser", {"network": {"policy": "public_web", "proxy": "127.0.0.1:3128"}}),
        ("profiles", "browser", {"network": {"policy": "public_web", "proxy": "10.0.0.1:3128"}}),
        ("profiles", "browser", {"concurrency": 65}),
        ("profiles", "browser", {"concurrency": "2"}),
        ("profiles", "browser", {"concurrency": True}),
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


def test_profiles_report_concurrency_with_one_by_default_for_jit(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = json.loads(json.dumps(CONFIG))
    config["profiles"]["session"]["concurrency"] = 2
    for entry in config["profiles"].values():
        launcher.validate_profile(entry)
    monkeypatch.setattr(launcher, "worker_config", lambda: config)
    profiles = launcher.handle({"action": "profiles"})["profiles"]
    assert profiles["browser"]["concurrency"] == 1  # jit, unset
    assert profiles["session"]["concurrency"] == 2  # jit, set by the host owner
    assert profiles["small"]["concurrency"] is None  # no jit, unset
    assert profiles["browser"]["queue_timeout_seconds"] == 600  # default


def test_profiles_report_a_configured_queue_timeout(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config: dict[str, Any] = json.loads(json.dumps(CONFIG))
    config["profiles"]["browser"]["queue_timeout_seconds"] = 900
    launcher.validate_profile(config["profiles"]["browser"])
    monkeypatch.setattr(launcher, "worker_config", lambda: config)
    reported = launcher.handle({"action": "profiles"})["profiles"]["browser"]
    assert reported["queue_timeout_seconds"] == 900


@pytest.mark.parametrize(
    ("machine", "release", "supported"),
    [
        ("x86_64", "6.12.107+deb13-cloud-amd64", True),
        ("x86_64", "6.12.111+deb13-amd64", True),
        ("x86_64", "6.12.48+deb13-amd64", True),
        ("x86_64", "6.12.111+deb13-arm64", False),
        ("x86_64", "6.13.1+deb13-amd64", False),
        ("x86_64", "6.1.0-40-cloud-amd64", False),
        ("x86_64", "6.12.111+deb13-rt-amd64", False),
        ("x86_64", "6.12.111+deb13-amd64 ", False),
        ("aarch64", "6.12.111+deb13-arm64", True),
        ("aarch64", "6.12.111+deb13-cloud-arm64", True),
        ("aarch64", "6.12.111+deb13-amd64", False),
        ("aarch64", "6.12.111+deb13-arm64-16k", False),
        ("aarch64", "6.12.111+deb13-rt-arm64", False),
        ("aarch64", "6.13.1+deb13-arm64", False),
        # Point releases: `+deb13.N`.
        ("aarch64", "6.12.115+deb13.1-cloud-arm64", True),
        ("aarch64", "6.12.115+deb13.1-arm64", True),
        ("x86_64", "6.12.115+deb13.1-amd64", True),
        ("x86_64", "6.12.115+deb13.1-cloud-amd64", True),
        ("x86_64", "6.12.115+deb13.1-cloud-arm64", False),
        ("aarch64", "6.12.115+deb13.1-cloud-amd64", False),
        ("x86_64", "6.12.115+deb13.1a-amd64", False),
        ("x86_64", "6.12.115+deb13.-amd64", False),
        ("x86_64", "6.12.115+deb13.1234-amd64", False),
        ("x86_64", "6.12.115+deb14-amd64", False),
        ("aarch64", "6.12.115+deb14.1-arm64", False),
    ],
)
def test_preflight_accepts_debian_13_6_12_kernels(
    machine: str, release: str, supported: bool
) -> None:
    assert launcher.supported_kernel(release, launcher.KERNEL_RELEASES[machine]) is supported


@pytest.mark.parametrize(
    ("machine", "release"),
    [("aarch64", "6.12.111+deb13-amd64"), ("x86_64", "6.12.111+deb13-arm64"), ("riscv64", "x")],
)
def test_preflight_refuses_a_kernel_for_another_architecture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: str, release: str
) -> None:
    path = tmp_path / "cardea-host.json"
    path.write_text(json.dumps({"kernel_uid": 990}))
    monkeypatch.setattr(launcher, "HOST_CONFIG", path)
    root_owned(monkeypatch, path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    real = Path.read_text
    monkeypatch.setattr(  # only /proc/1/comm is read before the architecture check
        Path, "read_text", lambda self, *a, **k: real(self) if self == path else "systemd"
    )
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(platform, "release", lambda: release)
    with pytest.raises(ValueError, match="unsupported_kernel"):
        launcher.preflight()


def root_owned(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real = Path.lstat

    def lstat(self: Path) -> os.stat_result:
        info = real(self)
        if self == path:
            return os.stat_result((info.st_mode & ~0o022, *info[1:4], 0, *info[5:]))
        return info

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.mark.parametrize(
    ("config", "valid"),
    [
        ({"kernel_uid": 990}, True),
        ({"kernel_uid": 990, "kernel_release_pattern": r"6\.12\.\d+\+deb13-rt-amd64"}, True),
        ({"kernel_uid": 990, "kernel_release_pattern": "("}, False),
        ({"kernel_uid": 990, "kernel_release_pattern": ""}, False),
        ({"kernel_uid": 990, "kernel_release_pattern": ["6.12.111+deb13-amd64"]}, False),
        ({"kernel_uid": 990, "other": 1}, False),
        ({"kernel_release_pattern": ".*"}, False),
    ],
)
def test_host_owner_may_set_the_kernel_release_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any], valid: bool
) -> None:
    path = tmp_path / "cardea-host.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(launcher, "HOST_CONFIG", path)
    root_owned(monkeypatch, path)
    if not valid:
        with pytest.raises(ValueError, match="invalid_configuration"):
            launcher.host_config()
        return
    pattern = launcher.host_config().get("kernel_release_pattern", launcher.KERNEL_RELEASE)
    assert launcher.supported_kernel("6.12.111+deb13-rt-amd64", pattern) is (
        "kernel_release_pattern" in config
    )


def test_host_configuration_must_be_root_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cardea-host.json"
    path.write_text(json.dumps({"kernel_uid": 990}))
    monkeypatch.setattr(launcher, "HOST_CONFIG", path)
    with pytest.raises(ValueError, match="unsafe_configuration"):
        launcher.host_config()


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

    assert run_worker(monkeypatch, capsys, refuse) == {
        "rejected": "blocked by robots.txt",
        "code": "operation_rejected",
    }

    class HTTPStatusError(Exception):
        def __init__(self) -> None:
            super().__init__("GET https://api.invalid/?token=s3cret: 404")
            self.response = type("Response", (), {"status_code": 404})()

    async def missing(arguments: dict[str, Any], context: OperationContext) -> dict[str, Any]:
        raise HTTPStatusError()

    # The message may carry URLs or secrets: it is scrubbed here, and only ever logged.
    assert run_worker(monkeypatch, capsys, missing) == {
        "error": {
            "type": "HTTPStatusError",
            "status": 404,
            "message": "GET https://api.invalid/?token=[redacted] 404",
        }
    }

    async def broken(arguments: dict[str, Any], context: OperationContext) -> dict[str, Any]:
        raise KeyError("url")

    assert run_worker(monkeypatch, capsys, broken) == {
        "error": {"type": "KeyError", "status": None, "message": "'url'"}
    }


def test_debian_probe_configuration_is_a_valid_host_owner_file() -> None:
    from test_confined_debian import config

    probes = config()
    for entry in probes["profiles"].values():
        launcher.validate_profile(entry)
    for entry in probes["workers"].values():
        launcher.validate_worker(entry, probes["profiles"])


# Output limits per job (ADR-023 amendment, 2026-11-16)

CODING = {**SMALL, "wall_seconds": 3600, "scratch_mb": 64, "output_kb": 16384}


def finish(job: str, content: bytes, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The status of a finished job whose worker wrote `content` as its output."""
    (launcher.ROOT / job / "output").write_bytes(content)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, "SubState=exited\nResult=success\nExecMainStatus=0\n", ""
        )

    monkeypatch.setattr(launcher, "run", run)
    return launcher.status(job)


@pytest.fixture
def with_coding(configured: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "profiles": {**cast(dict[str, Any], CONFIG["profiles"]), "coding": CODING},
        "workers": {
            **cast(dict[str, Any], CONFIG["workers"]),
            "code": {"argv": ["/usr/bin/true"], "profiles": ["coding"]},
        },
    }
    monkeypatch.setattr(launcher, "worker_config", lambda: config)


def test_a_profile_output_limit_holds_under_the_global_ceiling(
    with_coding: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launcher.handle(start("a" * 32, "small", "hash")) == {"started": True}
    assert (launcher.ROOT / ("a" * 32) / "output_limit").read_text() == str(64 * 1024)
    with pytest.raises(ValueError, match="invalid_output"):
        finish("a" * 32, b"x" * (64 * 1024 + 1), monkeypatch)
    exact = finish("a" * 32, b"y" * (64 * 1024), monkeypatch)
    assert len(base64.b64decode(exact["output"])) == 64 * 1024


def test_the_coding_profile_accepts_a_bundle_near_ten_mib(
    with_coding: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launcher.handle(start("b" * 32, "coding", "code")) == {"started": True}
    content = os.urandom(10 * 1048576 - 4096)
    result = finish("b" * 32, content, monkeypatch)
    assert base64.b64decode(result["output"]) == content


def test_a_job_without_a_recorded_limit_keeps_the_legacy_limit(
    helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = "c" * 32
    (launcher.ROOT / job).mkdir()
    with pytest.raises(ValueError, match="invalid_output"):
        finish(job, b"z" * (launcher.LEGACY_OUTPUT + 1), monkeypatch)


def test_short_reads_are_read_to_the_end_and_a_size_mismatch_is_refused(
    with_coding: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launcher.handle(start("d" * 32, "coding", "code")) == {"started": True}
    content = os.urandom(300000)
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, min(n, 1000)))
    assert base64.b64decode(finish("d" * 32, content, monkeypatch)["output"]) == content
    real_fstat = os.fstat

    def shrunk(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        fields = list(info)
        fields[6] = info.st_size - 1  # st_size: the file grew after fstat
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", shrunk)
    with pytest.raises(ValueError, match="invalid_output"):
        finish("d" * 32, content, monkeypatch)


# Staged inputs and the kernel callback for allow-list profiles (ADR-037)

VCS = {
    **BROWSER,
    "jit": False,
    "input_kb": 64,
    "kernel_callback": True,
    "network": {
        "policy": "allow_list",
        "proxy": "127.0.10.6:3128",
        "hosts": ["api.github.com", "github.com"],
    },
}


@pytest.fixture
def with_vcs(configured: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "profiles": {**cast(dict[str, Any], CONFIG["profiles"]), "vcs": VCS},
        "workers": {
            **cast(dict[str, Any], CONFIG["workers"]),
            "vcs": {"argv": ["/usr/bin/true"], "profiles": ["vcs"]},
        },
    }
    monkeypatch.setattr(launcher, "worker_config", lambda: config)


def staged(*contents: bytes) -> list[dict[str, str]]:
    return [
        {"sha256": hashlib.sha256(c).hexdigest(), "data": base64.b64encode(c).decode()}
        for c in contents
    ]


def test_staged_inputs_are_read_only_files_bound_read_only(
    with_vcs: None, configured: list[list[str]]
) -> None:
    job = "d" * 32
    started = launcher.handle(start(job, "vcs", "vcs", inputs=staged(b"bundle", b"two")))
    assert started == {"started": True}
    files = launcher.ROOT / job / "inputs"
    assert (files / "0").read_bytes() == b"bundle" and (files / "1").read_bytes() == b"two"
    assert (files / "0").stat().st_mode & 0o777 == 0o444
    command = configured[-1]
    at = command.index(str(files))
    assert command[at - 1 : at + 2] == ["--ro-bind", str(files), launcher.INPUTS]
    assert environment(command)["CARDEA_INPUTS"] == launcher.INPUTS
    # The gate callback is reachable; nothing else on loopback is named.
    assert properties(command)["IPAddressAllow"] == "127.0.10.6/32 127.0.11.1/32"


def test_a_job_without_inputs_binds_none(with_vcs: None, configured: list[list[str]]) -> None:
    launcher.handle(start("e" * 32, "vcs", "vcs"))
    assert "CARDEA_INPUTS" not in environment(configured[-1])
    assert not (launcher.ROOT / ("e" * 32) / "inputs").exists()


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (None, "input_digest_mismatch"),
        ([b"x" * (64 * 1024 + 1)], "inputs_too_large"),
        ([b"a" * 40000, b"b" * 40000], "inputs_too_large"),
        ([], "invalid_request"),
    ],
)
def test_staged_inputs_are_checked_before_the_fence(
    with_vcs: None, contents: list[bytes] | None, reason: str
) -> None:
    job = "f" * 32
    listed: Any = (
        [{"sha256": "0" * 64, "data": base64.b64encode(b"x").decode()}]
        if contents is None
        else staged(*contents)
    )
    with pytest.raises(ValueError, match=reason):
        launcher.handle(start(job, "vcs", "vcs", inputs=listed))
    assert not (launcher.ROOT / job).exists()
    with pytest.raises(ValueError, match="invalid_request"):
        launcher.handle(start(job, "vcs", "vcs", inputs=[{"data": "eA=="}]))


def test_a_profile_without_input_kb_takes_no_inputs(with_vcs: None) -> None:
    with pytest.raises(ValueError, match="inputs_too_large"):
        launcher.handle(start("a1" * 16, "browser", "fetch", inputs=staged(b"x")))


def test_kernel_callback_is_only_for_allow_list_profiles() -> None:
    launcher.validate_profile(VCS)
    for bad in (
        {**SESSION, "kernel_callback": True},
        {**SMALL, "kernel_callback": True},
        {**VCS, "kernel_callback": "yes"},
        {**VCS, "input_kb": launcher.MAX_STAGED // 1024 + 1},
    ):
        with pytest.raises(ValueError, match="invalid_profile"):
            launcher.validate_profile(bad)
    assert launcher.MAX_REQUEST > 4 * launcher.MAX_STAGED // 3


# Workers reach one dedicated loopback address, never 127.0.0.1 (ADR-028)


@pytest.mark.parametrize(("profile", "worker"), [("session", "agent"), ("vcs", "vcs")])
def test_endpoint_profiles_allow_only_the_dedicated_loopback_address(
    with_vcs: None, configured: list[list[str]], profile: str, worker: str
) -> None:
    launcher.handle(start("b2" * 16, profile, worker))
    command = configured[-1]
    allowed = properties(command)["IPAddressAllow"].split()
    assert f"{launcher.ENDPOINT_ADDRESS}/32" in allowed
    assert not any(a.startswith("127.0.0.1") or a == "127.0.0.0/8" for a in allowed)
    assert properties(command)["IPAddressDeny"] == "any"
    assert "127.0.0.1" not in " ".join(arg for arg in command if arg.startswith("--property="))
    assert environment(command)["NO_PROXY"].split(",")[0] == launcher.ENDPOINT_ADDRESS


def test_the_endpoint_address_is_apart_from_proxies_and_cannot_be_one() -> None:
    assert launcher.ENDPOINT_ADDRESS != "127.0.0.1"
    assert not launcher.ENDPOINT_ADDRESS.startswith("127.0.10.")
    with pytest.raises(ValueError, match="invalid_proxy"):
        network = {**cast(dict[str, Any], VCS["network"])}
        network["proxy"] = f"{launcher.ENDPOINT_ADDRESS}:3128"
        launcher.validate_profile({**VCS, "network": network})


def test_allow_list_profiles_without_the_callback_reach_no_loopback_endpoint(
    configured: list[list[str]],
) -> None:
    launcher.handle(start("b3" * 16, "browser", "fetch"))
    assert properties(configured[-1])["IPAddressAllow"] == "127.0.10.1/32"


# Package registries for code.session's gates (ADR-037 question 8)

REGISTRY_CODING = {
    **SESSION,
    "credential": "/etc/cardea/credentials/claude-code",
    "network": {
        "policy": "model_api_only",
        "proxy": "127.0.10.5:3128",
        "hosts": ["api.anthropic.com"],
        "registries": ["pypi.org", "files.pythonhosted.org"],
    },
}


@pytest.fixture
def with_registries(configured: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    config = {
        "profiles": {**cast(dict[str, Any], CONFIG["profiles"]), "coding": REGISTRY_CODING},
        "workers": {
            **cast(dict[str, Any], CONFIG["workers"]),
            "code-session": {"argv": ["/usr/bin/true"], "profiles": ["coding"]},
        },
    }
    monkeypatch.setattr(launcher, "worker_config", lambda: config)


def test_a_profile_may_list_package_registries(with_registries: None) -> None:
    launcher.validate_profile(REGISTRY_CODING)
    reported = launcher.handle({"action": "profiles"})["profiles"]["coding"]
    assert reported["network"]["registries"] == ["pypi.org", "files.pythonhosted.org"]
    # Absent or empty is the stricter option: the cache only, no registry.
    declared = cast(dict[str, Any], REGISTRY_CODING["network"])
    without = {k: v for k, v in declared.items() if k != "registries"}
    launcher.validate_profile({**REGISTRY_CODING, "network": without})
    launcher.validate_profile({**REGISTRY_CODING, "network": {**without, "registries": []}})
    browser = cast(dict[str, Any], BROWSER["network"])
    allow_list = {**BROWSER, "network": {**browser, "registries": ["pypi.org"]}}
    launcher.validate_profile(allow_list)


@pytest.mark.parametrize(
    "registries",
    [
        ["1.2.3.4"],
        ["127.0.0.1"],
        ["::1"],
        ["[::1]"],
        ["*.pythonhosted.org"],
        ["*"],
        ["pypi.org:443"],
        ["pypi.org:80"],
        ["https://pypi.org"],
        ["pypi.org/simple"],
        ["localhost"],
        ["PyPI.org"],
        ["pypi.org."],
        ["-pypi.org"],
        [""],
        ["pypi.org", "pypi.org"],
        [f"r{n}.example.org" for n in range(17)],
        "pypi.org",
        [7],
    ],
)
def test_registries_must_be_exact_hostnames(registries: object) -> None:
    network = {**cast(dict[str, Any], REGISTRY_CODING["network"]), "registries": registries}
    with pytest.raises(ValueError, match="invalid_network"):
        launcher.validate_profile({**REGISTRY_CODING, "network": network})


@pytest.mark.parametrize(
    "network",
    [
        {"policy": "none", "registries": ["pypi.org"]},
        {"policy": "public_web", "proxy": "127.0.10.3:3128", "registries": ["pypi.org"]},
    ],
)
def test_only_host_list_profiles_take_registries(network: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="invalid_network"):
        launcher.validate_profile({**SMALL, "network": network})


def test_registries_reach_the_proxy_rules_and_open_nothing_else(
    with_registries: None, configured: list[list[str]]
) -> None:
    network = cast(dict[str, Any], REGISTRY_CODING["network"])
    rules = launcher.egress_rules("coding", network)
    assert rules == [
        "acl coding_hosts dstdomain api.anthropic.com",
        "http_access allow coding_port coding_hosts",
        "acl coding_registries dstdomain pypi.org files.pythonhosted.org",
        # Registries over TLS only: CONNECT to port 443.
        "http_access allow coding_port coding_registries CONNECT tls_port",
    ]
    plain = {k: v for k, v in network.items() if k != "registries"}
    assert launcher.egress_rules("coding", plain) == rules[:2]
    # The unit itself still reaches only its proxy and the endpoint address: the
    # registries change what the proxy allows, never the unit's IP filter.
    launcher.handle(start("c1" * 16, "coding", "code-session"))
    props = properties(configured[-1])
    assert props["IPAddressDeny"] == "any"
    assert props["IPAddressAllow"].split() == ["127.0.10.5/32", f"{launcher.ENDPOINT_ADDRESS}/32"]
    assert "127.0.0.1" not in props["IPAddressAllow"]
    assert environment(configured[-1])["HTTPS_PROXY"] == "http://127.0.10.5:3128"


def test_the_helper_tells_the_worker_which_registries_its_proxy_allows(
    with_registries: None, configured: list[list[str]]
) -> None:
    """`code.session` refuses `dependencies: cache` on a profile with registries; the helper,
    not the host owner's worker entry, is what tells it."""
    launcher.handle(start("c2" * 16, "coding", "code-session"))
    assert environment(configured[-1])["CARDEA_REGISTRIES"] == "pypi.org,files.pythonhosted.org"
    # A networked profile without registries says so explicitly, with an empty list.
    launcher.handle(start("c3" * 16, "session", "agent"))
    assert environment(configured[-1])["CARDEA_REGISTRIES"] == ""
    # A worker entry cannot set it.
    worker = {"argv": ["/usr/bin/true"], "profiles": ["coding"], "env": {"CARDEA_REGISTRIES": ""}}
    with pytest.raises(ValueError, match="invalid_worker"):
        launcher.validate_worker(worker, {"coding": REGISTRY_CODING})


def test_scratch_may_not_exceed_memory_since_it_is_tmpfs() -> None:
    """Scratch pages are charged to the unit's memory (ADR-038 review)."""
    launcher.validate_profile({**BROWSER, "memory_mb": 512, "scratch_mb": 512})
    with pytest.raises(ValueError, match="invalid_profile"):
        launcher.validate_profile({**BROWSER, "memory_mb": 512, "scratch_mb": 513})


# Mount propagation (found on the first Debian acceptance run: a root-mode mmdebstrap in
# the host's namespace left every mount private, and systemd gave no unit its credential).


def mountinfo_text(root: str, run: str) -> str:
    def line(n: str, parent: str, point: str, tags: str, fs: str) -> str:
        return " ".join([n, parent, "0:1", "/", point, "rw", *tags.split(), "-", fs, fs, "rw"])

    return "\n".join(
        [
            line("22", "1", "/", root, "ext4"),
            line("24", "22", "/dev", "shared:2", "devtmpfs"),
            line("26", "22", "/run", run, "tmpfs"),
        ]
    )


@pytest.mark.parametrize(
    ("root", "run", "shared"),
    [
        ("shared:1", "shared:5", True),
        ("shared:1 master:3", "shared:5", True),
        ("", "shared:5", False),
        ("shared:1", "", False),
        ("master:3", "shared:5", False),
    ],
)
def test_shared_propagation_reads_the_shared_tags_of_root_and_run(
    tmp_path: Path, root: str, run: str, shared: bool
) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(mountinfo_text(root, run) + "\n")
    assert launcher.shared_propagation(mountinfo) is shared


def test_a_mount_on_top_decides_and_a_missing_run_is_not_shared(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    lines = mountinfo_text("shared:1", "shared:5")
    mountinfo.write_text(lines + "\n40 26 0:30 / /run rw - tmpfs tmpfs rw\n")
    assert launcher.shared_propagation(mountinfo) is False
    mountinfo.write_text(lines.splitlines()[0] + "\n")
    assert launcher.shared_propagation(mountinfo) is False


def test_a_start_is_refused_before_staging_when_mounts_are_not_shared(
    configured: list[list[str]], helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "shared_propagation", lambda: False)
    job = "d" * 32
    with pytest.raises(launcher.Rejected, match="^host_mount_propagation$"):
        launcher.handle(start(job, "session", "agent"))
    assert configured == [] and not (helper / job).exists()


@pytest.mark.parametrize(
    ("lines", "accepted"),
    [
        (["systemd=257.13-1~deb13u1", "bubblewrap=0.12.0-1~deb13u1"], True),
        # Later Debian stable or security updates of the same upstream and revision.
        (["bubblewrap=0.12.0-1~deb13u3", "systemd=257.13-1~deb13u2"], True),
        (["systemd=257.13-1~deb13u12", "bubblewrap=0.12.0-1~deb13u1"], True),
        # A new upstream or Debian revision needs fresh acceptance (ADR-018).
        (["systemd=257.14-1~deb13u1", "bubblewrap=0.12.0-1~deb13u1"], False),
        (["systemd=257.13-2", "bubblewrap=0.12.0-1~deb13u1"], False),
        (["systemd=257.13-1~deb13u1", "bubblewrap=0.12.0-2"], False),
        (["systemd=257.13-1~deb13u1", "bubblewrap=0.12.0-1~deb13u1~bpo1"], False),
        (["systemd=257.13-1~deb13u0", "bubblewrap=0.12.0-1~deb13u1"], False),
        (["systemd=257.13-1~deb13u1"], False),
        (["systemd=257.13-1~deb13u1", "bubblewrap=0.12.0-1~deb13u1", "bubblewrap=0.12.0"], False),
    ],
)
def test_probe_accepts_security_updates_of_the_fitted_packages(
    lines: list[str], accepted: bool
) -> None:
    assert launcher.accepted_packages(lines) is accepted


# The loopback phase (ADR-038 amendment, 2026-11-22): a second unit in a private network
# namespace, inheriting the first phase's workspace.
FETCHING = {**SMALL, "memory_mb": 2048, "scratch_mb": 256, "workspace_mb": 512, "network": {
    "policy": "allow_list",
    "proxy": "127.0.10.7:3128",
    "hosts": ["pypi.org"],
}}  # fmt: skip
LOOPBACK = {
    **SMALL,
    "memory_mb": 2048,
    "scratch_mb": 256,
    "network": {"policy": "loopback"},
    "inherit_workspace": True,
}
WORKER = {"argv": ["/opt/cardea/bin/python3", "-I", "-m", "x"], "read_only": ["/opt/cardea"]}


@pytest.fixture
def phases(configured: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    config = {
        "profiles": {"try": FETCHING, "try-loopback": LOOPBACK, "small": SMALL},
        "workers": {
            "fetch": {**WORKER, "profiles": ["try"]},
            "serve": {**WORKER, "profiles": ["try-loopback"]},
            "hash": {"argv": ["/usr/bin/sha256sum"], "profiles": ["small"]},
        },
    }
    monkeypatch.setattr(launcher, "worker_config", lambda: config)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=994, pw_gid=994))
    return configured


def test_the_loopback_unit_has_a_private_namespace_and_no_route_out(
    phases: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in the unit can name an address outside its own namespace's loopback."""
    layer = MountLayer(monkeypatch)  # no mount, chown or chmod: this runs without root
    first, second = "a" * 32, "b" * 32
    launcher.handle(start(first, "try", "fetch"))
    mounted = launcher.ROOT / first / "work"
    launcher.handle(start(second, "try-loopback", "serve", workspace_from=first))
    command = layer.commands[-1]
    props = properties(command)
    env = environment(command)
    # The namespace is the unit's own: `PrivateNetwork` stays, and with it only `lo`.
    assert props["PrivateNetwork"] == "yes"
    # TCP on that loopback, and only that: the cgroup filter is a second fence.
    assert props["IPAddressDeny"] == "any" and props["IPAddressAllow"] == "localhost"
    assert props["RestrictAddressFamilies"] == "AF_UNIX AF_INET AF_INET6"
    # No raw sockets or netlink to rebuild a route, and no capability to do either.
    assert props["CapabilityBoundingSet"] == "" and props["AmbientCapabilities"] == ""
    assert "AF_NETLINK" not in props["RestrictAddressFamilies"]
    assert props["NoNewPrivileges"] == "yes" and props["User"] == "cardea-worker"
    assert "--disable-userns" in command and "--unshare-net" not in command
    # No proxy, no kernel endpoint and no registry list: there is nothing to reach.
    assert not [name for name in env if name.upper().endswith("_PROXY")]
    assert "CARDEA_REGISTRIES" not in env
    assert not any(
        "127.0.10." in value or launcher.ENDPOINT_ADDRESS in value for value in props.values()
    )
    # The inherited workspace is the only thing it shares with the first phase.
    at = command.index(str(mounted))
    assert command[at - 1 : at + 2] == ["--bind", str(mounted), launcher.WORKSPACE]
    assert env["CARDEA_WORKSPACE"] == launcher.WORKSPACE


def _finished(commands: list[list[str]]) -> Any:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "SubState=exited\n", "")

    return run


class MountLayer:
    """The mount and ownership calls of both phases, recorded instead of performed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.commands: list[list[str]] = []
        self.mounted: list[Path] = []
        self.owner: dict[Path, tuple[int, int]] = {}
        self.mode: dict[Path, int] = {}
        monkeypatch.setattr(launcher, "run", self.run)
        monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) in self.mounted)
        monkeypatch.setattr(os, "chown", self.chown)
        monkeypatch.setattr(os, "chmod", self.chmod)

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[0] == "/usr/bin/mount":
            self.mounted.append(Path(args[-1]))
            self.owner[Path(args[-1])] = (994, 994)
        return subprocess.CompletedProcess(args, 0, "SubState=exited\n", "")

    def chown(self, path: Any, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        assert not follow_symlinks
        self.owner[Path(path)] = (uid, gid)

    def chmod(self, path: Any, mode: int, **kwargs: Any) -> None:
        self.mode[Path(path)] = mode


def test_the_second_phase_reuses_the_workspace_and_hands_it_to_the_worker(
    phases: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = MountLayer(monkeypatch)
    first, second = "a" * 32, "b" * 32
    launcher.handle(start(first, "try", "fetch"))
    work = launcher.ROOT / first / "work"
    # A root-owned root, as a helper running as root leaves one.
    layer.owner[work], layer.mode[work] = (0, 0o755), 0o755
    layer.commands.clear()
    launcher.handle(start(second, "try-loopback", "serve", workspace_from=first))
    # One mount in all, from the first phase; the second binds the same directory.
    assert not [c for c in layer.commands if c[0] == "/usr/bin/mount"]
    assert layer.mounted == [work] and not (launcher.ROOT / second / "work").exists()
    assert layer.owner[work] == (994, 994) and layer.mode[work] == 0o700
    command = layer.commands[-1]
    assert command[command.index(str(work)) - 1] == "--bind"
    # Handing the workspace over grants the second phase no credential.
    props = properties(command)
    assert not [v for v in props.values() if "Credential" in v or "credentials" in v]
    assert not [name for name in props if name == "LoadCredential"]
    assert not any("credential" in arg.lower() for arg in command)
    assert not [name for name in environment(command) if "CREDENTIAL" in name]


def test_a_loopback_scenario_can_create_its_home_in_the_workspace(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    catalog = cast(Any, SimpleNamespace(toolchain=None, env={}))
    env = try_.environment(work, catalog)
    assert (work / "home").is_dir() and env["HOME"] == str(work / "home")


def test_the_first_phase_keeps_its_proxy_and_gets_a_capped_workspace(
    phases: list[list[str]],
) -> None:
    job = "a" * 32
    launcher.handle(start(job, "try", "fetch"))
    mount, command = phases
    work = launcher.ROOT / job / "work"
    assert mount[:4] == ["/usr/bin/mount", "-t", "tmpfs", "-o"]
    assert mount[4] == "size=512m,mode=0700,uid=994,gid=994,nosuid,nodev"
    assert mount[-1] == str(work)
    props = properties(command)
    # Egress control is the profile's, unchanged.
    assert "PrivateNetwork" not in props and props["IPAddressAllow"] == "127.0.10.7/32"
    assert environment(command)["HTTPS_PROXY"] == "http://127.0.10.7:3128"
    assert command[command.index(str(work)) - 1] == "--bind"


def test_a_profile_without_a_workspace_is_unchanged(phases: list[list[str]]) -> None:
    launcher.handle(start("c" * 32, "small", "hash"))
    (command,) = phases
    assert "CARDEA_WORKSPACE" not in environment(command)
    assert launcher.WORKSPACE not in command
    assert not (launcher.ROOT / ("c" * 32) / "work").exists()


@pytest.mark.parametrize(
    ("profile", "worker", "change", "reason"),
    [
        ("try-loopback", "serve", {}, "invalid_workspace"),
        ("try-loopback", "serve", {"workspace_from": "z"}, "invalid_workspace"),
        ("try-loopback", "serve", {"workspace_from": "d" * 32}, "invalid_workspace"),
        ("try-loopback", "serve", {"workspace_from": "e" * 32}, "workspace_missing"),
        ("try", "fetch", {"workspace_from": "e" * 32}, "workspace_not_allowed"),
        ("small", "hash", {"workspace_from": "e" * 32}, "workspace_not_allowed"),
    ],
)
def test_an_inherited_workspace_is_checked_before_the_fence(
    phases: list[list[str]], profile: str, worker: str, change: dict[str, Any], reason: str
) -> None:
    job = "d" * 32
    with pytest.raises(launcher.Rejected, match=f"^{reason}$"):
        launcher.handle(start(job, profile, worker, **change))
    assert phases == [] and not (launcher.ROOT / job).exists()


def test_a_workspace_still_in_use_is_not_inherited(
    phases: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = "a" * 32
    launcher.handle(start(first, "try", "fetch"))
    mounted = launcher.ROOT / first / "work"
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == mounted)
    # The default fake reports the first unit as not finished.
    with pytest.raises(launcher.Rejected, match="^workspace_in_use$"):
        launcher.handle(start("b" * 32, "try-loopback", "serve", workspace_from=first))
    assert not (launcher.ROOT / ("b" * 32)).exists()


def test_cleanup_unmounts_a_workspace_before_deleting_anything(
    phases: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = "a" * 32
    launcher.handle(start(job, "try", "fetch"))
    work = launcher.ROOT / job / "work"
    (work / "left").write_text("x")
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == work)
    order: list[str] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        order.append(args[0])
        if args[0] == "/usr/bin/umount":
            assert (work / "left").exists()  # nothing deleted yet
        return subprocess.CompletedProcess(args, 0, "ActiveState=inactive\n", "")

    monkeypatch.setattr(launcher, "run", run)
    assert launcher.handle({"action": "cleanup", "job": job}) == {"cleaned": True}
    assert "/usr/bin/umount" in order and not (launcher.ROOT / job).exists()


def test_cleanup_is_refused_while_the_workspace_cannot_be_unmounted(
    phases: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = "a" * 32
    launcher.handle(start(job, "try", "fetch"))
    work = launcher.ROOT / job / "work"
    (work / "left").write_text("x")
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == work)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        busy = 1 if args[0] == "/usr/bin/umount" else 0
        return subprocess.CompletedProcess(args, busy, "ActiveState=inactive\n", "")

    monkeypatch.setattr(launcher, "run", run)
    with pytest.raises(ValueError, match="^workspace_busy$"):
        launcher.handle({"action": "cleanup", "job": job})
    assert (work / "left").exists()  # nothing was deleted through the mount


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_mb": 0},
        {"workspace_mb": 4096},  # more than the profile's memory
        {"workspace_mb": True},
        {"workspace_mb": 8, "inherit_workspace": True},
        {"workspace_mb": 8, "network": {"policy": "loopback"}},
    ],
)
def test_a_workspace_is_valid_only_on_a_networked_first_phase(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        launcher.validate_profile({**FETCHING, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"inherit_workspace": "yes"},
        {"network": {"policy": "none"}},
        {"network": {"policy": "loopback", "proxy": "127.0.10.7:3128"}},
        {"network": {"policy": "loopback", "hosts": ["pypi.org"]}},
    ],
)
def test_the_loopback_profile_takes_no_network_settings(change: dict[str, Any]) -> None:
    assert launcher.validate_profile(LOOPBACK) is LOOPBACK
    with pytest.raises(ValueError):
        launcher.validate_profile({**LOOPBACK, **change})


def test_the_loopback_policy_adds_nothing_to_the_egress_proxy() -> None:
    assert launcher.egress_rules("try-loopback", {"policy": "loopback"}) == []


def test_a_fresh_network_namespace_has_no_route_outside_loopback() -> None:
    """What `PrivateNetwork=yes` gives the unit, on this kernel: nothing but `lo`, no
    default route, and an address outside it is unreachable, not merely filtered. Skipped
    where the host forbids unprivileged namespaces; `env_try.sh` proves it in the unit."""
    if sys.platform != "linux":
        pytest.skip("network namespaces are Linux's")
    probe = (
        "import json, pathlib, socket\n"
        "routes = pathlib.Path('/proc/net/route').read_text().splitlines()[1:]\n"
        "table = pathlib.Path('/proc/net/dev').read_text().splitlines()[2:]\n"
        "devices = sorted(line.split(':')[0].strip() for line in table)\n"
        "answers = {}\n"
        "for label, host in (('external', '93.184.216.34'), ('metadata', '169.254.169.254'),\n"
        "                    ('host_proxy', '127.0.10.7')):\n"
        "    try:\n"
        "        socket.create_connection((host, 80), 2).close()\n"
        "        answers[label] = 'connected'\n"
        "    except OSError as error:\n"
        "        answers[label] = error.errno\n"
        "print(json.dumps({'routes': routes, 'devices': devices, 'answers': answers}))\n"
    )
    try:
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("unshare is not installed")
    if done.returncode:
        pytest.skip(f"this host does not allow a user and network namespace: {done.stderr[:100]}")
    seen = json.loads(done.stdout)
    assert seen["routes"] == [] and seen["devices"] == ["lo"]
    assert "connected" not in seen["answers"].values()
    # ENETUNREACH or EHOSTUNREACH: no route, not a refusal from something that answered.
    assert set(seen["answers"].values()) <= {101, 113}, seen


def test_a_bwrap_loopback_namespace_has_only_lo_and_no_route_out() -> None:
    """End to end, as the loopback unit's sandbox is built: bubblewrap's own network
    namespace. Needs root and bwrap; CI runs unprivileged and skips it. The owner runs it on
    the host (see the PR description)."""
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("needs root on a Linux host")
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("bwrap is not installed")
    probe = (
        "import pathlib, socket\n"
        "routes = pathlib.Path('/proc/net/route').read_text().splitlines()[1:]\n"
        "table = pathlib.Path('/proc/net/dev').read_text().splitlines()[2:]\n"
        "print(routes, sorted(line.split(':')[0].strip() for line in table))\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 80), 2).close()\n"
        "    print('connected')\n"
        "except OSError as error:\n"
        "    print(error.errno)\n"
    )
    done = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net",
         "--disable-userns", sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:2] == ["[] ['lo']", "101"], done.stdout
