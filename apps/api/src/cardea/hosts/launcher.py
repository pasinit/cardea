#!/usr/bin/python3 -I
"""Root-owned fixed-command helper. Install via the M2 host instructions, never setuid."""

import base64
import contextlib
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
# Debian 13's 6.12 LTS kernel, generic or cloud flavour, for each architecture the package is
# built for, with or without a point-release suffix (`+deb13` or `+deb13.N`). Point
# releases only bring fixes; the enforcement checks below (cgroup controllers, seccomp,
# bwrap) still run on every request. amd64 is the verified profile; arm64 is a development
# profile until a host fit passes on it (packaging/README.md).
# A host owner may replace the pattern with `kernel_release_pattern` in HOST_CONFIG.
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
KERNEL_RELEASES = {
    machine: rf"6\.12\.\d{{1,4}}\+deb13(\.\d{{1,3}})?(-cloud)?-{arch}"
    for machine, arch in ARCHITECTURES.items()
}
KERNEL_RELEASE = KERNEL_RELEASES["x86_64"]
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
# A worker's output document; artifacts such as a coding session's change bundle travel in it
# (ADR-037), so it may reach 16 MiB. Each profile's `output_kb` sets its own, smaller limit.
MAX_OUTPUT = 16 * 1048576
# The output limit of a job that recorded none: the fixed M2 workers, and jobs staged before
# per-job limits existed. It is the helper's former global limit.
LEGACY_OUTPUT = 262144
MAX_CONFINED_INPUT = 131072
# Staged inputs (ADR-037): object bytes the kernel hands a confined job, such as a change
# bundle. Written as read-only files in the job's staging, bound read-only into the
# sandbox, checked against their SHA-256, never recorded. Each profile's optional
# `input_kb` (default 0: none) caps their total, within this ceiling.
MAX_STAGED = 16 * 1048576
MAX_STAGED_FILES = 8
INPUTS = "/run/cardea/inputs"
# A request carries its staged inputs base64-encoded.
MAX_REQUEST = 262144 + 4 * (MAX_STAGED // 3 + 1) + 4096 * MAX_STAGED_FILES
NAME = r"[a-z][a-z0-9-]{0,31}"
LIMITS = {
    "memory_mb": (16, 16384),
    "cpu_percent": (1, 800),
    "tasks": (1, 1024),
    # Up to two hours: long confined operations such as a coding session (ADR-023 amendment).
    "wall_seconds": (1, 7200),
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
# `loopback`: a private network namespace with TCP on its own loopback and no other route:
# no proxy, no address of the host, nothing outside (ADR-038 amendment, 2026-11-22).
NETWORK = {"none", "model_api_only", "allow_list", "public_web", "loopback"}
HOSTNAME = r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
# Package registries a profile may reach for dependencies (ADR-037 question 8): exact
# hostnames only, reached over TLS on port 443. No wildcard, no port, no IP address (the
# last label is alphabetic) and no single-label name such as `localhost`.
REGISTRY_HOST = (
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
)
MAX_REGISTRIES = 16
NETWORK_KEYS = {"policy", "proxy", "hosts"}
# Read-only binds may expose installed software and public trust roots, never service state.
READ_ONLY_PREFIXES = ("/usr/", "/opt/", "/etc/ssl/", "/etc/fonts/")
ENV_NAME = r"[A-Z][A-Z0-9_]{0,63}"
RESERVED_ENV = {
    "PATH",
    "HOME",
    "CARDEA_DEADLINE",
    "CARDEA_CREDENTIAL",
    "CARDEA_CREDENTIALS",
    "CARDEA_INPUTS",
    "CARDEA_WORKSPACE",
    "CARDEA_REGISTRIES",
    "NO_PROXY",
}
# Tells a worker which package registries its profile's proxy allows (ADR-037 question 8),
# so `code.session` can refuse a configuration that claims fewer than the network allows.
REGISTRIES_ENV = "CARDEA_REGISTRIES"
CREDENTIAL = "/run/cardea/credential"
# A job's workspace (ADR-038 amendment): a size-capped tmpfs the helper mounts under the
# job's staging, bound writable here, which a later `loopback` job may inherit.
WORKSPACE = "/work"
WORKSPACE_MB = (1, 8192)
# A persistent tool cache (uv's packages, mypy's results) a profile may keep between jobs:
# a host directory under CACHE_ROOT, owned by the worker account, bound writable at CACHE.
# It only speeds jobs up; a job must not depend on what it holds.
# Under the helper's own state directory: the only place it may write when it runs inside the
# kernel service's namespace (ProtectSystem=strict leaves /var/cache read-only there).
CACHE_ROOT = ROOT / "cache"
CACHE = "/cache"
CACHE_NAME = r"[a-z][a-z0-9-]{0,31}"
# Packages of removed or older dependencies accumulate; past this a cache starts over empty,
# which costs one cold job.
CACHE_MAX_BYTES = 3 * 1024**3
# A profile's named credentials (ADR-037 amendment 2026-11-17): one file per name here.
CREDENTIALS = "/run/cardea/credentials"
CREDENTIAL_PATH = r"/etc/cardea/credentials/[a-z0-9-]{1,64}"
CREDENTIAL_NAME = r"[a-z][a-z0-9-]{0,31}"
MAX_CREDENTIALS = 8
# The one loopback address a confined worker may reach (ADR-028): the kernel's per-attempt
# tool endpoint and content-gate callback listen there, never on 127.0.0.1, so a worker
# (and an agent's own commands in it) cannot reach the kernel API or any other service on
# the host's loopback. It is outside 127.0.10.0/24, where the egress proxy ports live.
ENDPOINT_ADDRESS = "127.0.11.1"


def _proxy(value: object) -> tuple[str, int]:
    match = re.fullmatch(r"(127\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})):(\d{1,5})", str(value))
    if match is None or any(int(part) > 255 for part in match.groups()[1:4]):
        raise ValueError("invalid_proxy")
    address, port = match.group(1), int(match.group(5))
    # A proxy on 127.0.0.1 or the endpoint address would open what the fence closes.
    if address in {"127.0.0.1", ENDPOINT_ADDRESS} or not 0 < port < 65536:
        raise ValueError("invalid_proxy")
    return address, port


def validate_profile(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("invalid_profile")
    optional = {
        "jit",
        "credential",
        "credentials",
        "concurrency",
        "queue_timeout_seconds",
        "input_kb",
        "kernel_callback",
        "workspace_mb",
        "inherit_workspace",
        "cache",
    }
    if not {*LIMITS, "network"} <= set(entry) <= {*LIMITS, "network", *optional}:
        raise ValueError("invalid_profile")
    for key, (low, high) in LIMITS.items():
        value = entry[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid_profile")
    # Scratch is a tmpfs, whose pages are charged to the unit's memory: a scratch larger
    # than the memory cap could never be filled, and would mislead the host owner.
    if entry["scratch_mb"] > entry["memory_mb"]:
        raise ValueError("invalid_profile")
    if type(entry.get("jit", False)) is not bool:
        raise ValueError("invalid_profile")
    inputs = entry.get("input_kb", 0)
    if type(inputs) is not int or not 0 <= inputs <= MAX_STAGED // 1024:
        raise ValueError("invalid_profile")
    # The kernel's per-call loopback callback (the content gate) for an `allow_list`
    # profile; `model_api_only` profiles always reach their loopback tool endpoint.
    callback = entry.get("kernel_callback", False)
    declared = entry["network"] if isinstance(entry["network"], dict) else {}
    if type(callback) is not bool or (callback and declared.get("policy") != "allow_list"):
        raise ValueError("invalid_profile")
    # A workspace is handed from a networked first phase to a `loopback` second phase, never
    # the other way: what ran without a network cannot pass anything to a unit that has one.
    workspace = entry.get("workspace_mb")
    inherit = entry.get("inherit_workspace", False)
    if workspace is not None and (
        type(workspace) is not int
        or not WORKSPACE_MB[0] <= workspace <= min(WORKSPACE_MB[1], entry["memory_mb"])
        or inherit
        or declared.get("policy") == "loopback"
    ):
        raise ValueError("invalid_profile")
    if type(inherit) is not bool or (inherit and declared.get("policy") != "loopback"):
        raise ValueError("invalid_profile")
    cache = entry.get("cache")
    if cache is not None and (not isinstance(cache, str) or not re.fullmatch(CACHE_NAME, cache)):
        raise ValueError("invalid_profile")
    concurrency = entry.get("concurrency", 1)
    if type(concurrency) is not int or not CONCURRENCY[0] <= concurrency <= CONCURRENCY[1]:
        raise ValueError("invalid_profile")
    queue = entry.get("queue_timeout_seconds", DEFAULT_QUEUE_TIMEOUT)
    if type(queue) is not int or not QUEUE_TIMEOUT[0] <= queue <= QUEUE_TIMEOUT[1]:
        raise ValueError("invalid_profile")
    credential = entry.get("credential")
    if credential is not None and (
        not isinstance(credential, str) or not re.fullmatch(CREDENTIAL_PATH, credential)
    ):
        raise ValueError("invalid_profile")
    # Several named credentials instead of one, for a worker that acts as different
    # identities for different callers; the worker chooses by the kernel-resolved caller.
    named = entry.get("credentials")
    if named is not None and (
        credential is not None
        or not isinstance(named, dict)
        or not 1 <= len(named) <= MAX_CREDENTIALS
        or not all(
            isinstance(name, str)
            and re.fullmatch(CREDENTIAL_NAME, name)
            and isinstance(path, str)
            and re.fullmatch(CREDENTIAL_PATH, path)
            for name, path in named.items()
        )
        or len(set(named.values())) != len(named)
    ):
        raise ValueError("invalid_profile")
    network = entry["network"]
    if not isinstance(network, dict) or network.get("policy") not in NETWORK:
        raise ValueError("invalid_network")
    if network["policy"] in {"none", "loopback"}:
        if set(network) != {"policy"}:
            raise ValueError("invalid_network")
        return entry
    if network["policy"] == "public_web":
        if set(network) != {"policy", "proxy"}:
            raise ValueError("invalid_network")
        _proxy(network["proxy"])
        return entry
    hosts = network.get("hosts")
    registries = network.get("registries", [])
    if (
        not NETWORK_KEYS <= set(network) <= {*NETWORK_KEYS, "registries"}
        or not valid_registries(registries)
        or not isinstance(hosts, list)
        or not 1 <= len(hosts) <= 64
        or not all(isinstance(h, str) and re.fullmatch(HOSTNAME, h) for h in hosts)
    ):
        raise ValueError("invalid_network")
    _proxy(network["proxy"])
    return entry


def valid_registries(registries: object) -> bool:
    """A profile's optional registry allow-list: 1 to 16 distinct exact hostnames."""
    if registries == []:
        return True
    return (
        isinstance(registries, list)
        and 1 <= len(registries) <= MAX_REGISTRIES
        and len(set(registries)) == len(registries)
        and all(
            isinstance(h, str) and len(h) <= 253 and re.fullmatch(REGISTRY_HOST, h)
            for h in registries
        )
    )


def egress_rules(name: str, network: dict[str, Any]) -> list[str]:
    """The egress proxy's rules for one networked profile, as squid.conf lines.

    The unit reaches only its proxy (`IPAddressAllow`); these lines are what the proxy on
    that port then allows. `hosts` are the profile's service hosts. `registries` may be
    reached only by CONNECT to port 443, so a registry is TLS only. Every other destination,
    the host's own and private addresses included, falls to the file's deny rules.
    """
    if network["policy"] in {"none", "loopback"}:
        return []
    if network["policy"] == "public_web":
        return [f"http_access allow {name}_port"]
    lines = [
        f"acl {name}_hosts dstdomain {' '.join(network['hosts'])}",
        f"http_access allow {name}_port {name}_hosts",
    ]
    if network.get("registries"):
        lines += [
            f"acl {name}_registries dstdomain {' '.join(network['registries'])}",
            f"http_access allow {name}_port {name}_registries CONNECT tls_port",
        ]
    return lines


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
                # Names only, never paths.
                "credentials": sorted(entry.get("credentials") or {}),
                "input_kb": entry.get("input_kb", 0),
                "sha256": digest(entry),
            }
            for name, entry in config["profiles"].items()
        },
        "workers": {name: sorted(entry["profiles"]) for name, entry in config["workers"].items()},
        "sha256": digest(config),
    }


def confined_command(
    job: str,
    deadline: float,
    profile: dict[str, Any],
    worker: dict[str, Any],
    inputs: bool = False,
    workspace: Path | None = None,
) -> list[str]:
    """The M2 systemd + bubblewrap composition, parameterized only by host-owner values.

    `workspace` is a host directory bound writable at `/work` (ADR-038 amendment)."""
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
    if network["policy"] == "loopback":
        # Keeps `PrivateNetwork=yes`: the unit's own namespace, whose only interface is its
        # loopback. TCP needs the inet families; the cgroup filter then allows nothing but
        # loopback addresses, a second fence should the namespace ever not be private.
        props["RestrictAddressFamilies"] = "AF_UNIX AF_INET AF_INET6"
        props["SystemCallFilter"] = "~@debug @reboot @swap @obsolete @raw-io"
        props["IPAddressDeny"] = "any"
        props["IPAddressAllow"] = "localhost"
    elif network["policy"] != "none":
        address, port = _proxy(network["proxy"])
        del props["PrivateNetwork"]
        props["RestrictAddressFamilies"] = "AF_UNIX AF_INET AF_INET6"
        props["SystemCallFilter"] = "~@debug @reboot @swap @obsolete @raw-io"
        # Egress only to the host-owner proxy, which enforces the hostname allow-list.
        allowed = [f"{address}/32"]
        if network["policy"] == "model_api_only" or profile.get("kernel_callback"):
            allowed.append(f"{ENDPOINT_ADDRESS}/32")  # the per-attempt tool or gate endpoint
        props["IPAddressDeny"] = "any"
        props["IPAddressAllow"] = " ".join(allowed)
        url = f"http://{address}:{port}"
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            environment[key] = url
        environment["NO_PROXY"] = environment["no_proxy"] = f"{ENDPOINT_ADDRESS},localhost"
        # Always set for a networked unit, empty when the proxy allows no registry: the
        # worker can tell "no registries" from "not told".
        environment[REGISTRIES_ENV] = ",".join(network.get("registries", []))
    binds: list[str] = []
    loads: list[str] = []
    if profile.get("credential"):
        # systemd reads the root-only file; only this unit's user sees its copy.
        loads.append(f"cardea:{profile['credential']}")
        binds += ["--ro-bind", f"/run/credentials/{unit}/cardea", CREDENTIAL]
        environment["CARDEA_CREDENTIAL"] = CREDENTIAL
    if profile.get("credentials"):
        # One LoadCredential per name; the unit's credential directory holds them all.
        loads += [f"{name}:{path}" for name, path in sorted(profile["credentials"].items())]
        binds += ["--ro-bind", f"/run/credentials/{unit}", CREDENTIALS]
        environment["CARDEA_CREDENTIALS"] = CREDENTIALS
    if inputs:
        binds += ["--ro-bind", str(directory / "inputs"), INPUTS]
        environment["CARDEA_INPUTS"] = INPUTS
    if workspace is not None:
        binds += ["--bind", str(workspace), WORKSPACE]
        environment["CARDEA_WORKSPACE"] = WORKSPACE
    if profile.get("cache"):
        binds += ["--bind", str(cache_dir(profile["cache"])), CACHE]
        environment |= {
            "XDG_CACHE_HOME": CACHE,
            "UV_CACHE_DIR": f"{CACHE}/uv",
            "MYPY_CACHE_DIR": f"{CACHE}/mypy",
        }
    for path in worker.get("read_only", []):
        binds += ["--ro-bind", path, path]
    environment |= worker.get("env", {})
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        f"--unit={unit}",
        *[f"--property={k}={v}" for k, v in props.items()],
        *[f"--property=LoadCredential={load}" for load in loads],
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


# The accepted Debian 13 package lines: the fitted upstream and Debian revision, with any
# later Debian stable or security update of that same line (`~deb13uN`). A new upstream or
# Debian revision needs fresh acceptance evidence (ADR-018).
ACCEPTED_PACKAGES = {
    "systemd": r"257\.13-1~deb13u[1-9][0-9]{0,2}",
    "bubblewrap": r"0\.12\.0-1~deb13u[1-9][0-9]{0,2}",
}


def accepted_packages(lines: list[str]) -> bool:
    """Whether `dpkg-query` lines name exactly the accepted packages at accepted versions."""
    found = dict(line.split("=", 1) for line in lines if "=" in line)
    return (
        len(lines) == len(ACCEPTED_PACKAGES)
        and set(found) == set(ACCEPTED_PACKAGES)
        and all(re.fullmatch(pattern, found[name]) for name, pattern in ACCEPTED_PACKAGES.items())
    )


def preflight() -> dict[str, Any]:
    if platform.system() != "Linux" or Path("/proc/1/comm").read_text().strip() != "systemd":
        raise ValueError("unsupported_host")
    machine = platform.machine()
    if machine not in KERNEL_RELEASES:
        raise ValueError("unsupported_kernel")
    pattern = host_config().get("kernel_release_pattern", KERNEL_RELEASES[machine])
    if not supported_kernel(platform.release(), pattern):
        raise ValueError("unsupported_kernel")
    if 'VERSION_ID="13"' not in Path("/etc/os-release").read_text():
        raise ValueError("unsupported_host")
    versions = run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Package}=${Version}\n", "systemd", "bubblewrap"]
    ).stdout.splitlines()
    if not accepted_packages(versions):
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
    # The output is read once the worker has finished, not on every poll while it runs.
    if state == "finished" and path.exists():
        output = read_output(path, output_limit(job))
    return {
        "state": state,
        "result": props.get("Result", "unknown"),
        "exit_status": props.get("ExecMainStatus", "unknown"),
        "output": base64.b64encode(output).decode(),
        "cpu_ns": props.get("CPUUsageNSec", "[not set]"),
        "memory_peak": props.get("MemoryPeak", "[not set]"),
        "wall_ms": elapsed(props),
    }


def output_limit(job: str) -> int:
    """The job's recorded output limit, within the global ceiling; the legacy one if absent."""
    path = ROOT / job / "output_limit"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return LEGACY_OUTPUT
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 16:
            raise ValueError("invalid_output_limit")
        text = os.read(fd, 17).decode()
    finally:
        os.close(fd)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", text):
        raise ValueError("invalid_output_limit")
    return min(int(text), MAX_OUTPUT)


def read_output(path: Path, limit: int) -> bytes:
    """The whole output file, at most `limit` bytes; any other size is refused."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid_output")
        chunks: list[bytes] = []
        size = 0
        while True:
            # A short read is not the end: read until EOF, never past the limit.
            chunk = os.read(fd, min(1048576, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("invalid_output")
    finally:
        os.close(fd)
    if size != info.st_size:
        # The file changed while it was read: refuse rather than return part of it.
        raise ValueError("invalid_output")
    return b"".join(chunks)


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
    if sum(1 for p in ROOT.iterdir() if p.is_dir() and p != CACHE_ROOT) >= 256:
        raise ValueError("spool_capacity")
    return content


def admit_inputs(listed: object, profile: dict[str, Any]) -> list[bytes]:
    """Decode a job's staged inputs: each checked against its SHA-256, all together
    within the profile's `input_kb`."""
    if listed is None:
        return []
    if not isinstance(listed, list) or not 1 <= len(listed) <= MAX_STAGED_FILES:
        raise ValueError("invalid_request")
    limit = profile.get("input_kb", 0) * 1024
    decoded: list[bytes] = []
    for entry in listed:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "data"}
            or not isinstance(entry["data"], str)
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise ValueError("invalid_request")
        if len(entry["data"]) > 4 * (limit // 3 + 1):
            raise ValueError("inputs_too_large")
        data = base64.b64decode(entry["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("input_digest_mismatch")
        decoded.append(data)
        if sum(len(d) for d in decoded) > limit:
            raise ValueError("inputs_too_large")
    return decoded


JOB = r"[0-9a-f]{32}"


def admit_workspace(job: str, request: dict[str, Any], profile: dict[str, Any]) -> Path | None:
    """The first phase's workspace a `loopback` job inherits, checked before anything starts.

    Only a profile that inherits one may name one, and it must name a job whose staging is
    ours, whose workspace is a mount and whose unit has finished: no one else is writing.
    """
    source = request.get("workspace_from")
    if not profile.get("inherit_workspace"):
        if source is not None:
            raise ValueError("workspace_not_allowed")
        return None
    if not isinstance(source, str) or not re.fullmatch(JOB, source) or source == job:
        raise ValueError("invalid_workspace")
    directory, work = ROOT / source, ROOT / source / "work"
    if (
        directory.is_symlink()
        or work.is_symlink()
        or not work.is_dir()
        or directory.stat().st_uid != os.geteuid()
        or not host_mounted(work)
    ):
        raise ValueError("workspace_missing")
    shown = run(["/usr/bin/systemctl", "show", f"cardea-host-{source}.service"], check=False)
    state = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    if state.get("SubState") not in {"exited", "failed"}:
        raise ValueError("workspace_in_use")
    return work


# The host's mount namespace: where systemd starts the job units. The helper runs under
# sudo from the kernel's service, which has its own namespace (ProtectSystem, PrivateTmp),
# so a mount made there is invisible to the units: they see the bare, root-owned directory
# under it (`PermissionError: /work/home`). Workspace mounts are made, checked and removed
# in the host's namespace.
HOST_MOUNT_NAMESPACE = Path("/proc/1/ns/mnt")
HOST_MOUNTINFO = Path("/proc/1/mountinfo")


def in_host_namespace(args: list[str]) -> list[str]:
    """`args` run in the host's mount namespace: as they are when the helper already is
    there, otherwise through nsenter."""
    if not HOST_MOUNT_NAMESPACE.exists() or os.readlink("/proc/self/ns/mnt") == os.readlink(
        HOST_MOUNT_NAMESPACE
    ):
        return args
    return ["/usr/bin/nsenter", f"--mount={HOST_MOUNT_NAMESPACE}", "--", *args]


def host_mounted(path: Path, mountinfo: Path = HOST_MOUNTINFO) -> bool:
    """Whether `path` is a mount point in the host's namespace (field 5 of mountinfo, with
    its octal escapes)."""
    target = str(path)
    if not mountinfo.exists():
        # No /proc (not Linux): there is no other namespace to look into.
        return os.path.ismount(path)
    for line in mountinfo.read_text().splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4].encode().decode("unicode_escape") == target:
            return True
    return False


def _size(path: Path) -> int:
    """Bytes under `path`, without following links."""
    total = 0
    for root, _, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def cache_dir(name: str) -> Path:
    """The profile's persistent cache directory, created private to the worker account."""
    account = pwd.getpwnam("cardea-worker")
    CACHE_ROOT.mkdir(mode=0o711, exist_ok=True)
    path = CACHE_ROOT / name
    if path.is_symlink():
        raise ValueError("unsafe_cache")
    if path.is_dir() and _size(path) > CACHE_MAX_BYTES:
        shutil.rmtree(path)
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if (info.st_uid, info.st_gid) != (account.pw_uid, account.pw_gid):
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.chmod(path, 0o700)
    return path


def make_workspace(directory: Path, megabytes: int) -> Path:
    """A size-capped tmpfs under the job's staging, owned by the worker account. Its pages
    are charged to whoever fills them, the job's unit, so the unit's memory cap holds."""
    account = pwd.getpwnam("cardea-worker")
    work = directory / "work"
    work.mkdir(mode=0o700)
    options = f"size={megabytes}m,mode=0700,uid={account.pw_uid},gid={account.pw_gid},nosuid,nodev"
    run(in_host_namespace(["/usr/bin/mount", "-t", "tmpfs", "-o", options, "tmpfs", str(work)]))
    return work


def hand_over_workspace(work: Path) -> None:
    """Give an inherited workspace's mount root to the worker account for the second phase.

    Nothing is widened: the root stays private to the worker (0700) and is never followed
    through a link."""
    account = pwd.getpwnam("cardea-worker")
    os.chown(work, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.chmod(work, 0o700)


MOUNTINFO = Path("/proc/self/mountinfo")
# systemd sets up a unit's LoadCredential files through mount propagation. Once these are
# private (for example after a tool ran `mount --make-rprivate /` in the host's namespace),
# no unit gets its credential and bwrap fails on the missing source: refuse first instead.
PROPAGATED = ("/", "/run")


def shared_propagation(mountinfo: Path = MOUNTINFO) -> bool:
    """Whether every mount in `PROPAGATED` is shared (a `shared:N` tag, proc(5))."""
    tags: dict[str, list[str]] = {}
    for line in mountinfo.read_text().splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields[6:]:
            continue
        # Field 5 is the mount point; optional fields run from 7 up to the "-" separator.
        # A later line for the same mount point is the mount on top, the one in effect.
        tags[fields[4]] = fields[6 : fields.index("-", 6)]
    return all(any(t.startswith("shared:") for t in tags.get(p, [])) for p in PROPAGATED)


def admit_confined(
    job: str, request: dict[str, Any]
) -> tuple[bytes, dict[str, Any], dict[str, Any], list[bytes]]:
    preflight()
    config = worker_config()
    profile = config["profiles"].get(request["profile"])
    worker = config["workers"].get(request["worker"])
    if profile is None or worker is None or request["profile"] not in worker["profiles"]:
        raise ValueError("profile_not_installed")
    if not shared_propagation():
        raise ValueError("host_mount_propagation")
    if not isinstance(request["input"], str):
        raise ValueError("invalid_request")
    deadline = request["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
        raise ValueError("expired_authority")
    content = base64.b64decode(request["input"], validate=True)
    if len(content) > MAX_CONFINED_INPUT:
        raise ValueError("input_too_large")
    staged = admit_inputs(request.get("inputs"), profile)
    if retired(job):
        raise ValueError("retired_job")
    if sum(1 for p in ROOT.iterdir() if p.is_dir() and p != CACHE_ROOT) >= 256:
        raise ValueError("spool_capacity")
    return content, profile, worker, staged


def stage(
    directory: Path,
    content: bytes,
    output_limit: int = LEGACY_OUTPUT,
    staged: list[bytes] | None = None,
) -> None:
    """Stage a job: its input, its staged input files, and the largest output `status`
    will accept from it."""
    # Exclusive creation is a durable no-relaunch fence; a partial start is never retried.
    directory.mkdir(mode=0o711)
    directory.chmod(0o711)
    with (directory / "output_limit").open("x") as stream:
        stream.write(str(min(output_limit, MAX_OUTPUT)))
        stream.flush()
        os.fsync(stream.fileno())
    (directory / "output_limit").chmod(0o444)
    (directory / "input").mkdir(mode=0o755)
    (directory / "input").chmod(0o755)
    with (directory / "input/data").open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    (directory / "input/data").chmod(0o444)
    synced = [directory / "input", directory, ROOT]
    if staged:
        (directory / "inputs").mkdir(mode=0o755)
        (directory / "inputs").chmod(0o755)
        for index, data in enumerate(staged):
            with (directory / "inputs" / str(index)).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            (directory / "inputs" / str(index)).chmod(0o444)
        synced.insert(0, directory / "inputs")
    for path in synced:
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
        | ({"inputs"} if "inputs" in request else set())
        | ({"workspace_from"} if "workspace_from" in request else set())
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
            content, profile, worker, staged = admit_confined(job, request)
        except Exception as error:
            raise refusal(error) from error
        try:
            workspace = admit_workspace(job, request, profile)
        except Exception as error:
            raise refusal(error) from error
        # The job may return at most its profile's `output_kb`, whatever the global ceiling.
        stage(directory, content, profile["output_kb"] * 1024, staged)
        if request["deadline"] <= time.time():
            raise ValueError("expired_authority")
        if workspace is not None:
            hand_over_workspace(workspace)
        elif profile.get("workspace_mb"):
            workspace = make_workspace(directory, profile["workspace_mb"])
        run(confined_command(job, request["deadline"], profile, worker, bool(staged), workspace))
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
        work = directory / "work"
        if work.is_symlink():
            raise ValueError("unsafe_job")
        # Never delete through a mount: it goes first, or the cleanup is refused.
        if (
            host_mounted(work)
            and run(in_host_namespace(["/usr/bin/umount", str(work)]), check=False).returncode
        ):
            raise ValueError("workspace_busy")
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
