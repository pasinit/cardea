#!/usr/bin/python3 -I
"""Root-owned fixed-command helper. Install via the M2 host instructions, never setuid."""

import base64
import fcntl
import hashlib
import json
import math
import os
import platform
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

PROFILE = "debian-local-v1"
# Debian 13's 6.12 LTS kernel, generic or cloud flavour. Point releases only bring fixes; the
# enforcement checks below (cgroup controllers, seccomp, bwrap) still run on every request.
# A host owner may replace it with `kernel_release_pattern` in HOST_CONFIG.
KERNEL_RELEASE = r"6\.12\.\d{1,4}\+deb13(-cloud)?-amd64"
# Root-owned host-owner configuration: the kernel's uid and optional overrides.
HOST_CONFIG = Path("/etc/cardea-host.json")
ROOT = Path("/var/lib/cardea-host")
# Host-owner worker profiles and installed worker commands (ADR-028). Root-owned; a caller
# names a profile and a worker, never a command line, mount, limit or network rule.
WORKERS_CONFIG = Path("/etc/cardea-host-workers.json")
OPERATION = "host.sha256"
WORKER = (
    "import hashlib,json,sys,time;assert time.time()<float(sys.argv[1]);"
    "d=open('/input/data','rb').read();"
    "print(json.dumps({'sha256':hashlib.sha256(d).hexdigest(),'bytes':len(d)}))"
)
WORKERS = {
    OPERATION: WORKER,
    "host.copy": (
        "import base64,json,sys,time;assert time.time()<float(sys.argv[1]);"
        "d=open('/input/data','rb').read();"
        "print(json.dumps({'content_base64':base64.b64encode(d).decode(),'bytes':len(d)}))"
    ),
}
PROPERTIES = {
    "Type": "exec",
    "User": "cardea-worker",
    "Group": "cardea-worker",
    "NoNewPrivileges": "yes",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
    "PrivateNetwork": "yes",
    "RestrictRealtime": "yes",
    "RestrictAddressFamilies": "AF_UNIX",
    "SystemCallFilter": "~@network-io @debug @reboot @swap @obsolete @raw-io",
    "SystemCallErrorNumber": "EPERM",
    "SystemCallArchitectures": "native",
    "LockPersonality": "yes",
    "MemoryDenyWriteExecute": "yes",
    "WorkingDirectory": "/",
    "UMask": "0077",
    "MemoryMax": "64M",
    "MemorySwapMax": "0",
    "TasksMax": "8",
    "CPUQuota": "50%",
    "CPUQuotaPeriodSec": "100ms",
    "RuntimeMaxSec": "8s",
    "TimeoutStopSec": "1s",
    "KillMode": "control-group",
    "SendSIGKILL": "yes",
    "OOMPolicy": "kill",
    "LimitCORE": "0",
    "LimitFSIZE": "64K",
    "LimitNOFILE": "64",
    "StandardOutput": "null",
    "StandardError": "inherit",
    "RemainAfterExit": "yes",
    "StandardInput": "null",
}


# Confined worker profiles (ADR-028). The fixed M2 workers above are unchanged.
MAX_OUTPUT = 262144
MAX_REQUEST = 262144
MAX_CONFINED_INPUT = 131072
NAME = r"[a-z][a-z0-9-]{0,31}"
LIMITS = {
    "memory_mb": (16, 16384),
    "cpu_percent": (1, 800),
    "tasks": (1, 1024),
    "wall_seconds": (1, 3600),
    "scratch_mb": (1, 8192),
    "output_kb": (1, MAX_OUTPUT // 1024),
}
# Optional: how many workers of one profile the kernel runs at once. JIT profiles (browsers)
# default to one, so parallel tool calls cannot start several large processes together.
CONCURRENCY = (1, 64)
# Optional: how long a call may wait for a free slot of a limited profile before it fails
# as executor_busy. It never counts against the operation's own timeout.
QUEUE_TIMEOUT = (1, 3600)
DEFAULT_QUEUE_TIMEOUT = 600
# `public_web`: any public destination through the proxy, which must refuse non-public
# addresses (private, loopback, link-local, CGNAT, multicast, reserved); there is no host list.
NETWORK = {"none", "model_api_only", "allow_list", "public_web"}
HOSTNAME = r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
# Read-only binds may expose installed software and public trust roots, never service state.
READ_ONLY_PREFIXES = ("/usr/", "/opt/", "/etc/ssl/", "/etc/fonts/")
ENV_NAME = r"[A-Z][A-Z0-9_]{0,63}"
RESERVED_ENV = {"PATH", "HOME", "CARDEA_DEADLINE", "CARDEA_CREDENTIAL", "NO_PROXY"}
CREDENTIAL = "/run/cardea/credential"


def _proxy(value: object) -> tuple[str, int]:
    match = re.fullmatch(r"(127\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})):(\d{1,5})", str(value))
    if match is None or any(int(part) > 255 for part in match.groups()[1:4]):
        raise ValueError("invalid_proxy")
    address, port = match.group(1), int(match.group(5))
    # 127.0.0.1 carries agent tool endpoints; a proxy there would open every loopback port.
    if address == "127.0.0.1" or not 0 < port < 65536:
        raise ValueError("invalid_proxy")
    return address, port


def validate_profile(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("invalid_profile")
    optional = {"jit", "credential", "concurrency", "queue_timeout_seconds"}
    if not {*LIMITS, "network"} <= set(entry) <= {*LIMITS, "network", *optional}:
        raise ValueError("invalid_profile")
    for key, (low, high) in LIMITS.items():
        value = entry[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid_profile")
    if type(entry.get("jit", False)) is not bool:
        raise ValueError("invalid_profile")
    concurrency = entry.get("concurrency", 1)
    if type(concurrency) is not int or not CONCURRENCY[0] <= concurrency <= CONCURRENCY[1]:
        raise ValueError("invalid_profile")
    queue = entry.get("queue_timeout_seconds", DEFAULT_QUEUE_TIMEOUT)
    if type(queue) is not int or not QUEUE_TIMEOUT[0] <= queue <= QUEUE_TIMEOUT[1]:
        raise ValueError("invalid_profile")
    credential = entry.get("credential")
    if credential is not None and (
        not isinstance(credential, str)
        or not re.fullmatch(r"/etc/cardea/credentials/[a-z0-9-]{1,64}", credential)
    ):
        raise ValueError("invalid_profile")
    network = entry["network"]
    if not isinstance(network, dict) or network.get("policy") not in NETWORK:
        raise ValueError("invalid_network")
    if network["policy"] == "none":
        if set(network) != {"policy"}:
            raise ValueError("invalid_network")
        return entry
    if network["policy"] == "public_web":
        if set(network) != {"policy", "proxy"}:
            raise ValueError("invalid_network")
        _proxy(network["proxy"])
        return entry
    hosts = network.get("hosts")
    if (
        set(network) != {"policy", "proxy", "hosts"}
        or not isinstance(hosts, list)
        or not 1 <= len(hosts) <= 64
        or not all(isinstance(h, str) and re.fullmatch(HOSTNAME, h) for h in hosts)
    ):
        raise ValueError("invalid_network")
    _proxy(network["proxy"])
    return entry


def validate_worker(entry: object, profiles: dict[str, Any]) -> dict[str, Any]:
    keys = {"argv", "profiles", "read_only", "env"}
    if not isinstance(entry, dict) or not {"argv", "profiles"} <= set(entry) <= keys:
        raise ValueError("invalid_worker")
    argv, names = entry["argv"], entry["profiles"]
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 32
        or not all(isinstance(a, str) and 0 < len(a) <= 4096 and "\0" not in a for a in argv)
        or not argv[0].startswith(("/usr/", "/opt/"))
    ):
        raise ValueError("invalid_worker")
    if not isinstance(names, list) or not names or not all(n in profiles for n in names):
        raise ValueError("invalid_worker")
    paths = entry.get("read_only", [])
    if not isinstance(paths, list) or not all(
        isinstance(p, str)
        and p.startswith(READ_ONLY_PREFIXES)
        and ".." not in p.split("/")
        and "\0" not in p
        for p in paths
    ):
        raise ValueError("invalid_worker")
    env = entry.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str)
        and re.fullmatch(ENV_NAME, k)
        and k not in RESERVED_ENV
        and not k.endswith("_PROXY")
        and isinstance(v, str)
        and len(v) <= 1024
        for k, v in env.items()
    ):
        raise ValueError("invalid_worker")
    return entry


def worker_config() -> dict[str, Any]:
    """Validated host-owner configuration; absent means no confined profile is installed."""
    try:
        info = WORKERS_CONFIG.lstat()
    except FileNotFoundError:
        return {"profiles": {}, "workers": {}}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("unsafe_configuration")
    config = json.loads(WORKERS_CONFIG.read_bytes())
    if not isinstance(config, dict) or set(config) != {"profiles", "workers"}:
        raise ValueError("invalid_configuration")
    profiles, workers = config["profiles"], config["workers"]
    if not isinstance(profiles, dict) or not isinstance(workers, dict):
        raise ValueError("invalid_configuration")
    if not all(isinstance(n, str) and re.fullmatch(NAME, n) for n in (*profiles, *workers)):
        raise ValueError("invalid_configuration")
    for entry in profiles.values():
        validate_profile(entry)
    for entry in workers.values():
        validate_worker(entry, profiles)
    return config


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def installed_profiles() -> dict[str, Any]:
    """What the kernel may name: limits and network policy, never credential paths."""
    config = worker_config()
    return {
        "profiles": {
            name: {
                **{key: entry[key] for key in LIMITS},
                "network": entry["network"],
                "jit": entry.get("jit", False),
                # None: no per-profile limit beyond the kernel's own queues.
                "concurrency": entry.get("concurrency", 1 if entry.get("jit") else None),
                "queue_timeout_seconds": entry.get("queue_timeout_seconds", DEFAULT_QUEUE_TIMEOUT),
                "credential": entry.get("credential") is not None,
                "sha256": digest(entry),
            }
            for name, entry in config["profiles"].items()
        },
        "workers": {name: sorted(entry["profiles"]) for name, entry in config["workers"].items()},
        "sha256": digest(config),
    }


def confined_command(
    job: str, deadline: float, profile: dict[str, Any], worker: dict[str, Any]
) -> list[str]:
    """The M2 systemd + bubblewrap composition, parameterized only by host-owner values."""
    directory = ROOT / job
    unit = f"cardea-host-{job}.service"
    scratch = profile["scratch_mb"] * 1048576
    remaining = min(float(profile["wall_seconds"]), max(0.001, deadline - time.time()))
    props = PROPERTIES | {
        # Arguments arrive on stdin and the result leaves on stdout, both as JSON.
        "StandardInput": f"file:{directory}/input/data",
        "StandardOutput": f"file:{directory}/output",
        "MemoryMax": f"{profile['memory_mb']}M",
        "TasksMax": str(profile["tasks"]),
        "CPUQuota": f"{profile['cpu_percent']}%",
        "RuntimeMaxSec": f"{remaining:.3f}s",
        "TimeoutStopSec": "2s",
        "LimitFSIZE": str(max(scratch, profile["output_kb"] * 1024)),
        "LimitNOFILE": "1024",
    }
    if profile.get("jit"):
        # V8 and other JITs need writable-executable mappings (Chromium, Node).
        props["MemoryDenyWriteExecute"] = "no"
    environment = {"PATH": "/usr/bin", "HOME": "/tmp", "CARDEA_DEADLINE": repr(deadline)}
    network = profile["network"]
    if network["policy"] != "none":
        address, port = _proxy(network["proxy"])
        del props["PrivateNetwork"]
        props["RestrictAddressFamilies"] = "AF_UNIX AF_INET AF_INET6"
        props["SystemCallFilter"] = "~@debug @reboot @swap @obsolete @raw-io"
        # Egress only to the host-owner proxy, which enforces the hostname allow-list.
        allowed = [f"{address}/32"]
        if network["policy"] == "model_api_only":
            allowed.append("127.0.0.1/32")  # the per-attempt Cardea tool endpoint
        props["IPAddressDeny"] = "any"
        props["IPAddressAllow"] = " ".join(allowed)
        url = f"http://{address}:{port}"
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            environment[key] = url
        environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost"
    binds: list[str] = []
    if profile.get("credential"):
        # systemd reads the root-only file; only this unit's user sees its copy.
        props["LoadCredential"] = f"cardea:{profile['credential']}"
        binds += ["--ro-bind", f"/run/credentials/{unit}/cardea", CREDENTIAL]
        environment["CARDEA_CREDENTIAL"] = CREDENTIAL
    for path in worker.get("read_only", []):
        binds += ["--ro-bind", path, path]
    environment |= worker.get("env", {})
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        f"--unit={unit}",
        *[f"--property={k}={v}" for k, v in props.items()],
        "--",
        "/usr/bin/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        *binds,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--remount-ro",
        "/dev",
        "--size",
        str(scratch),
        "--tmpfs",
        "/tmp",
        "--chdir",
        "/tmp",
        "--remount-ro",
        "/",
        "--clearenv",
        *[arg for key, value in environment.items() for arg in ("--setenv", key, value)],
        "--",
        *worker["argv"],
    ]


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=check,
        timeout=5,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
    )


def host_config() -> dict[str, Any]:
    info = HOST_CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("unsafe_configuration")
    config = json.loads(HOST_CONFIG.read_text())
    if not isinstance(config, dict) or not {"kernel_uid"} <= set(config) <= {
        "kernel_uid",
        "kernel_release_pattern",
    }:
        raise ValueError("invalid_configuration")
    pattern = config.get("kernel_release_pattern", KERNEL_RELEASE)
    if not isinstance(pattern, str) or not 0 < len(pattern) <= 256:
        raise ValueError("invalid_configuration")
    try:
        re.compile(pattern)
    except re.error:
        raise ValueError("invalid_configuration") from None
    return config


def supported_kernel(release: str, pattern: str = KERNEL_RELEASE) -> bool:
    return re.fullmatch(pattern, release) is not None


def preflight() -> dict[str, Any]:
    if platform.system() != "Linux" or Path("/proc/1/comm").read_text().strip() != "systemd":
        raise ValueError("unsupported_host")
    pattern = host_config().get("kernel_release_pattern", KERNEL_RELEASE)
    if platform.machine() != "x86_64" or not supported_kernel(platform.release(), pattern):
        raise ValueError("unsupported_kernel")
    if 'VERSION_ID="13"' not in Path("/etc/os-release").read_text():
        raise ValueError("unsupported_host")
    versions = run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Package}=${Version}\n", "systemd", "bubblewrap"]
    ).stdout.splitlines()
    if set(versions) != {"systemd=257.13-1~deb13u1", "bubblewrap=0.12.0-1~deb13u1"}:
        raise ValueError("unsupported_host_versions")
    if not {"cpu", "memory", "pids"} <= set(
        Path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
    ):
        raise ValueError("unsupported_controllers")
    if (
        not os.access("/sys/fs/cgroup", os.W_OK)
        or "Seccomp:" not in Path("/proc/self/status").read_text()
    ):
        raise ValueError("unsupported_enforcement")
    if Path("/usr/bin/bwrap").stat().st_mode & stat.S_ISUID:
        raise ValueError("setuid_launcher_denied")
    if pwd.getpwnam("cardea-worker").pw_uid == 0:
        raise ValueError("privileged_worker")
    return {
        "profile": PROFILE,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def command(job: str, deadline: float, operation: str = OPERATION) -> list[str]:
    directory = ROOT / job
    props = PROPERTIES | {
        "StandardOutput": f"file:{directory}/output",
        "RuntimeMaxSec": f"{min(8.0, max(0.001, deadline - time.time())):.3f}s",
    }
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        f"--unit=cardea-host-{job}.service",
        *[f"--property={k}={v}" for k, v in props.items()],
        "--",
        "/usr/bin/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        str(directory / "input"),
        "/input",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--remount-ro",
        "/dev",
        "--size",
        "1048576",
        "--tmpfs",
        "/tmp",
        "--chdir",
        "/tmp",
        "--remount-ro",
        "/",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--",
        "/usr/bin/python3",
        "-I",
        "-c",
        WORKERS[operation],
        str(deadline),
    ]


def status(job: str) -> dict[str, Any]:
    unit = f"cardea-host-{job}.service"
    result = run(["/usr/bin/systemctl", "show", unit], check=False)
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    state = "unknown"
    if props.get("SubState") in {"exited", "failed"}:
        state = "finished"
    elif props.get("ActiveState") in {"active", "activating", "deactivating"}:
        state = "running"
    output = b""
    path = ROOT / job / "output"
    if path.exists():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OUTPUT:
                raise ValueError("invalid_output")
            output = os.read(fd, MAX_OUTPUT + 1)
        finally:
            os.close(fd)
    return {
        "state": state,
        "result": props.get("Result", "unknown"),
        "exit_status": props.get("ExecMainStatus", "unknown"),
        "output": base64.b64encode(output).decode(),
        "cpu_ns": props.get("CPUUsageNSec", "[not set]"),
        "memory_peak": props.get("MemoryPeak", "[not set]"),
        "wall_ms": elapsed(props),
    }


def elapsed(props: dict[str, str]) -> int | None:
    try:
        start = int(props["ExecMainStartTimestampMonotonic"])
        end = int(props["ExecMainExitTimestampMonotonic"])
        if 0 < start <= end:
            return min(3600000, (end - start + 999) // 1000)
    except (KeyError, ValueError):
        pass
    return None


def retired(job: str, *, record: bool = False) -> bool:
    path = ROOT / ".retired.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_history")
    finally:
        os.close(fd)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("PRAGMA synchronous=FULL")
        database.execute("CREATE TABLE IF NOT EXISTS retired (job TEXT PRIMARY KEY)")
        if record:
            database.execute("INSERT OR IGNORE INTO retired VALUES (?)", (job,))
        exists = (
            database.execute("SELECT 1 FROM retired WHERE job=?", (job,)).fetchone() is not None
        )
    if record:
        fd = os.open(ROOT, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return exists


class Rejected(ValueError):
    """Refused before the no-relaunch fence: no staging, unit or effect exists."""


def admit_start(job: str, request: dict[str, Any]) -> bytes:
    preflight()
    if request["operation"] not in WORKERS or not isinstance(request["input"], str):
        raise ValueError("unknown_operation")
    deadline = request["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
        raise ValueError("expired_authority")
    content = base64.b64decode(request["input"], validate=True)
    if len(content) > 16384:
        raise ValueError("input_too_large")
    if retired(job):
        raise ValueError("retired_job")
    if sum(1 for p in ROOT.iterdir() if p.is_dir()) >= 256:
        raise ValueError("spool_capacity")
    return content


def admit_confined(
    job: str, request: dict[str, Any]
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    preflight()
    config = worker_config()
    profile = config["profiles"].get(request["profile"])
    worker = config["workers"].get(request["worker"])
    if profile is None or worker is None or request["profile"] not in worker["profiles"]:
        raise ValueError("profile_not_installed")
    if not isinstance(request["input"], str):
        raise ValueError("invalid_request")
    deadline = request["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
        raise ValueError("expired_authority")
    content = base64.b64decode(request["input"], validate=True)
    if len(content) > MAX_CONFINED_INPUT:
        raise ValueError("input_too_large")
    if retired(job):
        raise ValueError("retired_job")
    if sum(1 for p in ROOT.iterdir() if p.is_dir()) >= 256:
        raise ValueError("spool_capacity")
    return content, profile, worker


def stage(directory: Path, content: bytes) -> None:
    # Exclusive creation is a durable no-relaunch fence; a partial start is never retried.
    directory.mkdir(mode=0o711)
    directory.chmod(0o711)
    (directory / "input").mkdir(mode=0o755)
    (directory / "input").chmod(0o755)
    with (directory / "input/data").open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    (directory / "input/data").chmod(0o444)
    for path in (directory / "input", directory, ROOT):
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def refusal(error: Exception) -> "Rejected":
    reason = str(error) if isinstance(error, ValueError) else ""
    return Rejected(reason if re.fullmatch(r"[a-z_]{1,64}", reason) else "start_rejected")


def handle(request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "probe" and set(request) == {"action"}:
        return preflight()
    if action == "profiles" and set(request) == {"action"}:
        preflight()
        return installed_profiles()
    job = request.get("job")
    if not isinstance(job, str) or not re.fullmatch(r"[0-9a-f]{32}", job):
        raise Rejected("invalid_job")
    required = (
        {"action", "job", "input", "operation", "deadline"}
        if action == "start"
        else {"action", "job", "input", "profile", "worker", "deadline"}
        if action == "start_confined"
        else {"action", "job"}
    )
    actions = {"start", "start_confined", "status", "stop", "cleanup"}
    if set(request) != required or action not in actions:
        raise Rejected("invalid_request")
    directory = ROOT / job
    if action == "start":
        try:
            content = admit_start(job, request)
        except Exception as error:
            raise refusal(error) from error
        deadline = request["deadline"]
        stage(directory, content)
        if deadline <= time.time():
            raise ValueError("expired_authority")
        run(command(job, deadline, request["operation"]))
        return {"started": True}
    if action == "start_confined":
        try:
            content, profile, worker = admit_confined(job, request)
        except Exception as error:
            raise refusal(error) from error
        stage(directory, content)
        if request["deadline"] <= time.time():
            raise ValueError("expired_authority")
        run(confined_command(job, request["deadline"], profile, worker))
        return {"started": True}
    if action == "status":
        return status(job)
    if action == "cleanup":
        # Cleanup never stops a live unit on behalf of a caller: the kernel must reconcile first.
        shown = run(["/usr/bin/systemctl", "show", f"cardea-host-{job}.service"], check=False)
        values = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        if (
            values.get("ActiveState") not in {"inactive", "failed"}
            and values.get("LoadState") != "not-found"
        ):
            raise ValueError("cleanup_active")
        group = Path("/sys/fs/cgroup/system.slice") / f"cardea-host-{job}.service" / "cgroup.procs"
        if group.exists() and group.read_text().strip():
            raise ValueError("descendants_remain")
        if directory.is_symlink():
            raise ValueError("unsafe_job")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("unsupported_cleanup")
        retired(job, record=True)  # Commit the replay fence before deleting anything.
        if directory.exists():
            if directory.stat().st_uid != os.geteuid():
                raise ValueError("unsafe_job")
            shutil.rmtree(directory)
        fd = os.open(ROOT, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        run(["/usr/bin/systemctl", "reset-failed", f"cardea-host-{job}.service"], check=False)
        return {"cleaned": True}
    before = status(job)
    unit = f"cardea-host-{job}.service"
    # Stop even if staging is absent: a partial failure may have created the unit.
    stopped = run(["/usr/bin/systemctl", "stop", unit], check=False)
    after = run(["/usr/bin/systemctl", "show", unit], check=False).stdout
    props = dict(line.split("=", 1) for line in after.splitlines() if "=" in line)
    if (
        props.get("ActiveState") not in {"inactive", "failed"}
        and props.get("LoadState") != "not-found"
    ):
        raise ValueError("stop_unconfirmed")
    group = Path("/sys/fs/cgroup/system.slice") / unit / "cgroup.procs"
    if group.exists() and group.read_text().strip():
        raise ValueError("descendants_remain")
    if stopped.returncode and props.get("LoadState") != "not-found":
        raise ValueError("stop_failed")
    return before | {"stopped": True}


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise ValueError("root_fixed_command_required")
    # sudo authenticates the kernel OS account; agents/workers have no sudo rule.
    allowed = host_config()["kernel_uid"]
    if int(os.environ.get("SUDO_UID", "-1")) != allowed:
        raise ValueError("caller_denied")
    ROOT.mkdir(mode=0o711, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("unsafe_spool")
    ROOT.chmod(0o711)
    data = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(data) > MAX_REQUEST:
        raise ValueError("request_too_large")
    request = json.loads(data)
    if not isinstance(request, dict):
        raise ValueError("invalid_request")
    with (ROOT / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        response = handle(request)
    print(json.dumps(response))


if __name__ == "__main__":
    try:
        main()
    except Rejected as rejected:
        print(json.dumps({"error": str(rejected), "rejected": True}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": "host_request_failed"}))
        raise SystemExit(1) from None
