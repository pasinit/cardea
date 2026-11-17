"""Shared harness for the Debian acceptance scripts (docs/acceptance/debian-adr-034-037.md).

Every script follows the same rules:

- **Its own store only.** A script works in one directory it creates. The directory holds
  a sentinel naming the script. A script refuses a directory that exists without its
  sentinel, so it can never open the host's store or another script's.
- **Idempotent.** Running it again with the same directory wipes what the previous run
  created there and starts over. It refuses while a process from that run is still alive.
- **Its own processes only.** Every process it starts is recorded with its PID. It
  signals only those PIDs, never a process found by name.
- **Free ports.** Listeners bind a port the kernel picks at random on loopback, unless the
  operator names an address.
- **Evidence.** Each script writes one JSON document: the checks, each true or false, the
  observations behind them, and the versions it ran.

The kernel code is the installed package's (`/usr/lib/cardea` by default). Scripts that
start a daemon start the real `serve()` through `launcher.py`, with the UNENFORCED
development identity of `experiments/try` and the scripted harness in `plan.py`, because
an acceptance run must not need SPIRE or a model.
"""

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
TRY = REPOSITORY / "experiments" / "try"
REFERENCE_OPS = REPOSITORY / "packages" / "cardea-reference-ops" / "src"
INSTALLED = Path("/usr/lib/cardea")
SENTINEL = ".cardea-acceptance"
DOMAIN = "acceptance.invalid"
# Paths a script never takes as its working directory, whatever the operator passes.
FORBIDDEN = tuple(
    Path(p) for p in ("/", "/etc", "/usr", "/var", "/var/lib", "/var/lib/cardea", "/home", "/root")
)


def add_common_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    parser.add_argument(
        "--work",
        type=Path,
        default=Path("/var/tmp/cardea-acceptance") / name,
        help="the directory this script creates and owns (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where to write the evidence JSON (default: WORK/evidence.json)",
    )
    parser.add_argument(
        "--cardea-lib",
        type=Path,
        default=None,
        help="the kernel package to test (default: /usr/lib/cardea if installed, "
        "else this checkout's apps/api/src)",
    )


def use_kernel(lib: Path | None) -> Path:
    """Put the kernel package under test first on the import path; return its directory.

    The default is the Debian package's `/usr/lib/cardea`. Without it, a `cardea` already
    importable by this interpreter (the small-host kit's environment) is used, and only
    then this checkout's `apps/api/src`.
    """
    if lib is None and (INSTALLED / "cardea").is_dir():
        lib = INSTALLED
    if lib is None:
        try:
            import cardea

            return Path(cardea.__file__).resolve().parents[1]
        except ImportError:
            lib = REPOSITORY / "apps" / "api" / "src"
    if not (lib / "cardea").is_dir():
        raise SystemExit(f"no cardea package under {lib}")
    sys.path.insert(0, str(lib))
    return lib


# The working directory


def claim(work: Path, name: str) -> Path:
    """Create `work` for script `name`, or reuse it if that script created it before.

    Refuses a directory without this script's sentinel, a forbidden path, and a directory
    whose previous run still has a live process. A reused directory is emptied first.
    """
    work = work.absolute()
    if work in FORBIDDEN or work == Path.home():
        raise SystemExit(f"refusing to use {work}: choose a scratch directory")
    marker = work / SENTINEL
    if work.exists() or work.is_symlink():
        if work.is_symlink() or not work.is_dir():
            raise SystemExit(f"refusing {work}: not a plain directory")
        if not marker.is_file() or marker.read_text().strip() != name:
            raise SystemExit(
                f"refusing {work}: this script did not create it (no {SENTINEL} naming {name})"
            )
        alive = [pid for pid in recorded_pids(work) if is_ours(pid, work)]
        if alive:
            raise SystemExit(f"refusing {work}: processes {alive} of an earlier run are alive")
        shutil.rmtree(work)
    work.mkdir(parents=True, mode=0o700)
    work.chmod(0o700)
    marker.write_text(name + "\n")
    return work


def recorded_pids(work: Path) -> list[int]:
    try:
        return [int(p) for p in json.loads((work / "pids.json").read_text())]
    except (OSError, ValueError):
        return []


def is_ours(pid: int, work: Path) -> bool:
    """Whether `pid` is alive and still the process a script started in `work`."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # No /proc (not Linux): trust the PID file only while the process exists.
        return True
    return str(work).encode() in cmdline


def free_port(host: str = "127.0.0.1") -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def wait_for[T](check: Callable[[], T | None], timeout: float, interval: float = 0.5) -> T:
    """Call `check` until it returns a true value (not None, False or empty) without raising."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
            if value:
                return value
            error: Exception | None = None
        except Exception as raised:
            error = raised
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f} s: {error or 'condition false'}")
        time.sleep(interval)


@contextmanager
def read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a store, for checking what the kernel committed."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def rows(path: Path, sql: str, *params: Any) -> list[dict[str, Any]]:
    with read_only(path) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


# Processes this script starts


class Processes:
    """Every process a script starts, recorded so that only those are ever signalled."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self.started: list[subprocess.Popen[bytes]] = []

    def start(self, argv: list[str], log: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        stream = log.open("ab")
        process = subprocess.Popen(argv, env=env, stdout=stream, stderr=subprocess.STDOUT)
        stream.close()
        self.started.append(process)
        (self.work / "pids.json").write_text(json.dumps([p.pid for p in self.started]))
        return process

    @staticmethod
    def kill(process: subprocess.Popen[bytes]) -> None:
        """SIGKILL one process this script started: a crash, not a shutdown."""
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
        process.wait(timeout=30)

    @staticmethod
    def stop(process: subprocess.Popen[bytes], grace: float = 20) -> None:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGKILL)
                process.wait(timeout=30)

    def close(self) -> None:
        for process in self.started:
            self.stop(process)


# Evidence


class Evidence:
    def __init__(self, script: str, work: Path, out: Path | None) -> None:
        self.path = out or work / "evidence.json"
        self.document: dict[str, Any] = {
            "script": script,
            "started_at": datetime.now(UTC).isoformat(),
            "host": {
                "machine": platform.machine(),
                "release": platform.release(),
                "python": platform.python_version(),
                "os_release": _os_release(),
            },
            "commit": _commit(),
            "checks": {},
            "observations": {},
        }

    @property
    def checks(self) -> dict[str, bool]:
        return self.document["checks"]  # type: ignore[no-any-return]

    def check(self, name: str, passed: bool, **observed: Any) -> bool:
        self.checks[name] = bool(passed)
        if observed:
            self.observe(name, observed)
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)
        return bool(passed)

    def observe(self, name: str, value: Any) -> None:
        self.document["observations"][name] = value

    def write(self, error: str | None = None) -> int:
        self.document["finished_at"] = datetime.now(UTC).isoformat()
        if error:
            self.document["error"] = error
        passed = bool(self.checks) and all(self.checks.values()) and not error
        self.document["passed"] = passed
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.document, indent=2, sort_keys=True, default=str) + "\n"
        )
        temporary.replace(self.path)
        print(json.dumps({"passed": passed, "evidence": str(self.path)}), flush=True)
        return 0 if passed else 1


def _os_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


def _commit() -> str | None:
    head = REPOSITORY / ".git"
    try:
        done = subprocess.run(
            ["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    if not head.exists():
        return None
    return done.stdout.strip() or None


def run_script(evidence: Evidence, body: Callable[[], None]) -> int:
    """Run the script body; an exception is recorded in the evidence, not lost."""
    try:
        body()
    # SystemExit too: the stack raises it when its daemon exits early, and that run still
    # owes its evidence.
    except (Exception, SystemExit) as error:  # noqa: BLE001 - every failure belongs in the evidence
        return evidence.write(f"{type(error).__name__}: {error}")
    return evidence.write()
