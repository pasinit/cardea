"""Debian-only enforcement probes for confined worker profiles (ADR-028).

Skipped unless ``CARDEA_CONFINED_DEBIAN=1`` on Linux. Run them in the disposable M2 Debian
13 guest as the unprivileged kernel account, with the M2 helper installed from this source
tree and these test-only profiles installed by root:

    python3 -I apps/api/tests/hosts/test_confined_debian.py > /etc/cardea-host-workers.json
    chmod 0644 /etc/cardea-host-workers.json   # root-owned, not group/world writable

The ``probe-allow`` profile needs an HTTP CONNECT proxy on 127.0.10.1:3128 that allows only
``example.org`` (for example Squid with ``acl allowed dstdomain example.org``). The probe
worker runs fixed probes chosen by name; it never executes caller-supplied code. Install it
only on a disposable test host.
"""

import asyncio
import json
import os
import platform
import time
import uuid
from typing import Any

import pytest

from cardea.service.confined import WorkerFailed, Workers

PROBE = r"""
import json, os, socket, sys
name = json.load(sys.stdin)["arguments"]["probe"]
result = {}
if name == "memory":
    blocks = []
    for _ in range(64):
        blocks.append(bytearray(8 * 1024 * 1024))
        blocks[-1][::4096] = b"x" * len(blocks[-1][::4096])
    result["allocated_mb"] = 8 * len(blocks)
elif name == "network":
    for target in (("1.1.1.1", 443), ("127.0.0.1", 22)):
        try:
            socket.create_connection(target, timeout=3).close()
            result[target[0]] = "connected"
        except OSError as error:
            result[target[0]] = type(error).__name__
    try:
        socket.getaddrinfo("example.org", 443)
        result["dns"] = "resolved"
    except OSError as error:
        result["dns"] = type(error).__name__
elif name == "proxy":
    proxy = os.environ["HTTPS_PROXY"].removeprefix("http://")
    host, port = proxy.rsplit(":", 1)
    for target in ("example.org:443", "example.com:443"):
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
                result[target] = s.recv(64).split(b"\r\n")[0].decode()
        except OSError as error:
            result[target] = type(error).__name__
elif name == "store":
    for path in ("/var/lib/cardea", "/var/lib/cardea-host", "/etc/cardea-host.json",
                 "/etc/cardea-host-workers.json", "/etc/cardea", "/home", "/root",
                 "/run/cardea/credential"):
        result[path] = os.path.exists(path)
print(json.dumps({"output": result}))
"""


def config() -> dict[str, Any]:
    small = {
        "memory_mb": 64,
        "cpu_percent": 50,
        "tasks": 8,
        "wall_seconds": 20,
        "scratch_mb": 1,
        "output_kb": 16,
    }
    return {
        "profiles": {
            "probe-none": small | {"network": {"policy": "none"}},
            "probe-allow": small
            | {
                "network": {
                    "policy": "allow_list",
                    "proxy": "127.0.10.1:3128",
                    "hosts": ["example.org"],
                }
            },
        },
        "workers": {
            "probe": {
                "argv": ["/usr/bin/python3", "-I", "-c", PROBE],
                "profiles": ["probe-none", "probe-allow"],
            }
        },
    }


pytestmark = pytest.mark.skipif(
    os.environ.get("CARDEA_CONFINED_DEBIAN") != "1" or platform.system() != "Linux",
    reason="Debian host enforcement probes; set CARDEA_CONFINED_DEBIAN=1 on the M2 guest",
)


def probe(profile: str, name: str) -> dict[str, Any]:
    from cardea.hosts.debian import DebianHost

    host = DebianHost()
    host.probe()
    workers = Workers(host)
    workers.require(profile, "probe")
    return asyncio.run(
        workers.run(
            uuid.uuid4().hex,
            {"arguments": {"probe": name}},
            profile=profile,
            worker="probe",
            deadline=time.time() + 15,
        )
    )


def test_memory_cap_kills_the_worker_and_reports_it() -> None:
    with pytest.raises(WorkerFailed) as killed:
        probe("probe-none", "memory")
    assert killed.value.code == "worker_memory_limit"


def test_network_none_denies_every_connection() -> None:
    result = probe("probe-none", "network")
    assert result["1.1.1.1"] != "connected" and result["127.0.0.1"] != "connected"
    assert result["dns"] != "resolved"


def test_allow_list_reaches_only_the_proxy_and_allowed_hosts() -> None:
    direct = probe("probe-allow", "network")
    assert direct["1.1.1.1"] != "connected" and direct["127.0.0.1"] != "connected"
    proxied = probe("probe-allow", "proxy")
    assert " 200" in proxied["example.org:443"]
    assert " 200" not in proxied["example.com:443"]


def test_worker_cannot_see_the_store_or_host_configuration() -> None:
    seen = probe("probe-none", "store")
    assert not any(seen.values()), seen


if __name__ == "__main__":
    print(json.dumps(config(), indent=2))
