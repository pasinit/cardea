"""Bounded root-admin fixture for a disposable Debian VM; never run on a user host."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from systemd_profile import PROFILE, PROPERTIES, ROOT, command

RESULTS: list[dict[str, Any]] = []


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, timeout=20, check=check)


def properties(unit: str) -> dict[str, str]:
    data = run(["systemctl", "show", unit]).stdout
    return dict(line.split("=", 1) for line in data.splitlines() if "=" in line)


def wait[T](check: Callable[[], T | None], timeout: float = 20) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition timed out")


def launch(code: str, **limits: str) -> str:
    unit = "cardea-fit-" + uuid.uuid4().hex + ".service"
    result = run(
        command(unit, code, StandardOutput=f"file:{ROOT}/results/{unit}.out", **limits),
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return unit


def finish(unit: str) -> tuple[dict[str, str], str]:
    state = wait(
        lambda: p if (p := properties(unit)).get("ActiveState") in {"inactive", "failed"} else None
    )
    journal = run(["journalctl", "--unit", unit, "--no-pager", "--output=cat", "-n", "50"]).stdout
    output = ROOT / "results" / f"{unit}.out"
    assert output.stat().st_size <= 65536
    assert "not known, ignoring" not in journal, journal
    return state, output.read_text(errors="replace")


def execute(name: str, code: str, **limits: str) -> tuple[dict[str, str], str]:
    started = time.monotonic()
    unit = launch(code, **limits)
    try:
        state, journal = finish(unit)
        result = {
            "name": name,
            "unit": unit,
            "result": state.get("Result"),
            "exit_status": state.get("ExecMainStatus"),
            "output_excerpt": journal[:1024],
            "output_bytes": len(journal.encode()),
            "output_sha256": hashlib.sha256(journal.encode()).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        RESULTS.append(result)
        return state, journal
    finally:
        run(["systemctl", "stop", unit], check=False)
        run(["systemctl", "reset-failed", unit], check=False)


def success(name: str, code: str) -> None:
    state, journal = execute(name, code + '\nprint("FIT_OK", flush=True)')
    assert state["Result"] == "success" and "FIT_OK" in journal.splitlines(), (
        name,
        state.get("Result"),
        journal,
    )
    RESULTS[-1]["passed"] = True


def preflight() -> dict[str, Any]:
    errors = []
    release = Path("/etc/os-release")
    if platform.system() != "Linux" or not release.exists():
        errors.append("Debian 13 Linux required")
    elif "ID=debian" not in release.read_text() or 'VERSION_ID="13"' not in release.read_text():
        errors.append("Debian 13 required")
    if not Path("/proc/1/comm").exists() or Path("/proc/1/comm").read_text().strip() != "systemd":
        errors.append("systemd as PID 1 required")
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    if not controllers.exists() or not {"cpu", "memory", "pids"} <= set(
        controllers.read_text().split()
    ):
        errors.append("cgroup v2 CPU, memory and pids controllers required")
    if not os.access("/sys/fs/cgroup", os.W_OK):
        errors.append("writable cgroup hierarchy required for the admin fit fixture")
    if not all(shutil.which(name) for name in ("systemd-run", "systemctl", "journalctl", "bwrap")):
        errors.append("systemd tools and bubblewrap required")
    return {"eligible_for_fit": not errors, "errors": errors, "enforcement_verified": False}


def prepare() -> None:
    os.environ["CARDEA_FAKE_CREDENTIAL"] = "synthetic-not-a-real-secret"
    assert os.geteuid() == 0
    assert Path("/etc/cardea-disposable-m2-vm").read_text().strip() == "yes"
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    assert Path("/etc/os-release").read_text().find("ID=debian") >= 0
    assert Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    for name in ("input", "secrets", "results"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    (ROOT / "input/data.json").write_text('{"authorized": true}')
    # Synthetic markers only: no real credentials are copied into the guest.
    (ROOT / "secrets/credential").write_text("synthetic-private-token")
    link = ROOT / "input/escape"
    if not link.is_symlink():
        link.symlink_to(ROOT / "secrets/credential")


def main() -> None:
    assert preflight()["eligible_for_fit"], preflight()
    prepare()
    success(
        "authorized_input_unprivileged",
        """
import os,json
assert os.getuid() == 65534
assert os.getppid() == 1
assert json.load(open('/input/data.json')) == {'authorized': True}
status=open('/proc/self/status').read()
assert 'NoNewPrivs:\\t1' in status
assert 'CapEff:\\t0000000000000000' in status
print(json.dumps({'uid':os.getuid()}))
""",
    )
    success(
        "filesystem_secrets_symlink_and_readonly_denial",
        """
import os
for path in ['/etc/shadow','/root/.ssh','/var/lib/cardea-m2-fit/secrets/credential',
             '/input/escape','/input/../var/lib/cardea-m2-fit/secrets/credential',
             '/run/dbus/system_bus_socket',
             '/proc/self/root/var/lib/cardea-m2-fit/secrets/credential']:
    try: open(path,'rb').read()
    except OSError: pass
    else: raise AssertionError(path)
for path in ['/input/data.json','/usr/worker-escape','/etc/worker-escape',
             '/sys/fs/cgroup/cgroup.procs','/dev/worker-escape','/dev/shm/worker-escape']:
    try: open(path,'w').write('escape')
    except OSError: pass
    else: raise AssertionError(path)
assert 'CARDEA_FAKE_CREDENTIAL' not in os.environ
assert not os.path.exists('/sys/fs/cgroup')
assert not os.path.exists('/oldroot')

""",
    )
    success(
        "network_and_unix_socket_denial",
        """
import socket
for family in [socket.AF_INET,socket.AF_INET6,socket.AF_UNIX]:
    try: socket.socket(family,socket.SOCK_STREAM)
    except OSError: pass
    else: raise AssertionError(family)
""",
    )
    success(
        "scratch_file_size_limit",
        """
import os
try:
    with open('/tmp/output','wb') as f: f.write(b'x'*131072)
except OSError: pass
else: raise AssertionError('file limit absent')
assert os.stat('/tmp/output').st_size <= 65536
""",
    )
    success(
        "scratch_is_private_and_capacity_bounded",
        """
import os
assert not os.path.exists('/tmp/output')
written=0
try:
    for i in range(32):
        with open('/tmp/f'+str(i),'wb') as f:
            f.write(b'x'*65536);written+=65536
except OSError: pass
else: raise AssertionError('scratch capacity absent')
assert written <= 1048576
""",
    )
    success(
        "privilege_namespace_and_executable_memory_denial",
        """
import ctypes,os,mmap,errno
try: os.setuid(0)
except OSError: pass
else: raise AssertionError('privilege escalation')
libc=ctypes.CDLL(None,use_errno=True)
assert libc.unshare(0x10000000) == -1
assert ctypes.get_errno() == errno.ENOSPC  # bubblewrap --disable-userns exhausts nested quota
try: mmap.mmap(-1,4096,prot=mmap.PROT_READ|mmap.PROT_WRITE|mmap.PROT_EXEC)
except OSError: pass
else: raise AssertionError('writable executable memory')
""",
    )
    success(
        "process_count_limit",
        """
import os,time
children=[]
try:
    for i in range(16):
        try: pid=os.fork()
        except OSError: break
        if pid==0:
            time.sleep(2);os._exit(0)
        children.append(pid)
    else: raise AssertionError('process limit absent')
    assert len(children) < 8
finally:
    for pid in children: os.waitpid(pid,0)
""",
    )
    state, _ = execute("stdout_byte_limit", "import os;os.write(1,b'x'*131072);os.write(1,b'y')")
    assert state["Result"] != "success"
    RESULTS[-1]["passed"] = True
    state, _ = execute("memory_limit", "data=bytearray(256*1024*1024)")
    assert state["Result"] == "oom-kill", state
    RESULTS[-1]["passed"] = True
    state, _ = execute("wall_deadline", "import time;time.sleep(60)", RuntimeMaxSec="2s")
    assert state["Result"] == "timeout", state
    RESULTS[-1]["passed"] = True
    unit = launch(
        """
import os,time,signal
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if os.fork()==0:
    os.setsid()
    if os.fork()==0:
        print("TREE_READY", flush=True)
        while True: pass
    os._exit(0)
while True: pass
""",
        RuntimeMaxSec="30s",
    )
    try:
        state = wait(
            lambda: (
                p
                if (p := properties(unit)).get("ActiveState") == "active"
                and "TREE_READY" in (ROOT / "results" / f"{unit}.out").read_text()
                else None
            )
        )
        group = Path("/sys/fs/cgroup") / state["ControlGroup"].lstrip("/")
        quota = (group / "cpu.max").read_text().strip()
        maximum, period = quota.split()
        assert maximum != "max" and int(maximum) / int(period) == 0.5, quota
        assert (group / "memory.max").read_text().strip() == str(64 * 1024 * 1024)
        assert (group / "pids.max").read_text().strip() == "8"
        wait(
            lambda: (
                int(
                    dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())[
                        "nr_throttled"
                    ]
                )
                > 0
            )
        )
        pids = (group / "cgroup.procs").read_text().split()
        assert len(pids) >= 2
        run(["systemctl", "stop", unit])
        wait(lambda: not group.exists() or not (group / "cgroup.procs").read_text().strip())
        assert all(not Path("/proc", pid).exists() for pid in pids)
        RESULTS.append(
            {
                "name": "cgroup_controls_and_detached_descendant_cancel",
                "passed": True,
                "cpu_max": quota,
                "cpu_throttling_observed": True,
                "pids_observed": len(pids),
            }
        )
    finally:
        run(["systemctl", "stop", unit], check=False)
        run(["systemctl", "reset-failed", unit], check=False)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        result = preflight()
        print(json.dumps(result))
        raise SystemExit(0 if result["eligible_for_fit"] else 78)
    report: dict[str, Any] = {
        "profile": PROFILE,
        "properties": PROPERTIES,
        "checks": RESULTS,
        "kernel": platform.release(),
        "machine": platform.machine(),
        "debian": Path("/etc/debian_version").read_text().strip(),
        "systemd": run(["systemctl", "--version"]).stdout.splitlines()[0],
        "python": platform.python_version(),
        "bubblewrap": run(["bwrap", "--version"]).stdout.strip(),
        "packages": run(
            [
                "dpkg-query",
                "-W",
                "-f=${Package}=${Version}\n",
                "systemd",
                "libsystemd-shared",
                "bubblewrap",
                "libseccomp2",
                "python3",
                "linux-image-cloud-amd64",
            ]
        ).stdout.splitlines(),
    }
    try:
        main()
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["failure"] = repr(error)
        raise
    finally:
        report["sources"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path(__file__), Path(__file__).with_name("systemd_profile.py"))
        }
        Path("/root/host-fit-result.json").write_text(json.dumps(report, indent=2) + "\n")
