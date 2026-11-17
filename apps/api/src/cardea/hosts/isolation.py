"""Isolation of untrusted steps inside a confined worker (ADR-028, ADR-037).

A worker that runs untrusted code (a coding agent's harness, the gates that execute the
agent's change, a throwaway environment's scenarios) shares its uid and PID namespace with
that code. Three things keep the code from forging what the worker reports:

- `harden()`: the worker is made non-dumpable (`PR_SET_DUMPABLE`, 0), so same-uid code
  cannot open `/proc/<worker>/fd/N` or read its memory, and a child subreaper
  (`PR_SET_CHILD_SUBREAPER`), so a descendant that detaches with `setsid` and outlives
  its parent is reparented to the worker and can still be found. Both are read back.
- `run_step()`: one untrusted command, in its own session and process group, with its
  output read by threads so a detached descendant holding a pipe cannot stall the worker.
  When it exits, or on its timeout, every descendant of the worker that the step left is
  killed with SIGKILL through a pidfd, so a reused PID is never signalled.
- `clear()`: the same check on its own, before the next step and before reporting. A
  descendant that is still alive after the kill rounds raises `IsolationFailed`
  (`isolation_failed` by default; callers may pass their own code).

Not prevented here: same-uid code can still signal the worker itself (the profiles'
seccomp filter blocks `@debug`, so `ptrace` and `pidfd_getfd`, but not `kill`). A killed
worker reports nothing, and the kernel records a worker failure, never a forged result.
"""

import contextlib
import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
# Kill rounds before a descendant counts as surviving, and the pause between them.
ROUNDS = 50
PAUSE = 0.1
# Bytes of a step's standard output and error kept (each); the rest is read and dropped.
MAX_OUTPUT = 1 << 20


class IsolationFailed(Exception):
    """A step's descendant survived, or the worker could not be hardened."""

    def __init__(self, message: str, code: str = "isolation_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Step:
    """One untrusted command's outcome. `killed`: stray descendants it left, now killed."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    killed: int
    duration_ms: int


def _prctl(option: int, value: int = 0) -> int:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    result = int(libc.prctl(option, value, 0, 0, 0))
    if result < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return result


def harden(code: str = "isolation_failed") -> str:
    """Make this process non-dumpable and a child subreaper; return the mode in effect.

    On Linux both settings are read back, and `IsolationFailed(code)` is raised if either
    did not take. Elsewhere (development platforms only) there is neither, and the mode is
    `process_group`: steps are still killed by group, but a detached descendant may escape.
    """
    if not sys.platform.startswith("linux"):
        return "process_group"
    try:
        _prctl(PR_SET_DUMPABLE, 0)
        _prctl(PR_SET_CHILD_SUBREAPER, 1)
        dumpable = _prctl(PR_GET_DUMPABLE)
        reaper = ctypes.c_int(0)
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(reaper), 0, 0, 0) < 0:
            raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
    except OSError as error:
        raise IsolationFailed(f"the worker could not be hardened: {error}", code) from None
    if dumpable != 0 or reaper.value != 1:
        raise IsolationFailed("the worker could not be hardened", code)
    return "subreaper"


def _processes() -> dict[int, tuple[int, str, int]]:
    """Every visible process: pid to (parent pid, state, process group)."""
    table: dict[int, tuple[int, str, int]] = {}
    proc = Path("/proc")
    if proc.is_dir() and (proc / "self" / "stat").exists():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            # The command name is in parentheses and may contain spaces or parentheses.
            fields = stat[stat.rindex(")") + 2 :].split()
            table[int(entry.name)] = (int(fields[1]), fields[0], int(fields[2]))
        return table
    # Without /proc (development platforms): `ps`, which lists itself, so it is left out.
    lister = subprocess.Popen(
        ["ps", "-A", "-o", "pid=,ppid=,stat=,pgid="], stdout=subprocess.PIPE, text=True
    )
    listed, _ = lister.communicate()
    for line in listed.splitlines():
        parts = line.split()
        numbers = len(parts) >= 4 and all(parts[i].isdigit() for i in (0, 1, 3))
        if numbers and int(parts[0]) != lister.pid:
            table[int(parts[0])] = (int(parts[1]), parts[2][0], int(parts[3]))
    return table


def descendants(root: int | None = None, alive_only: bool = True) -> list[int]:
    """The processes below `root` (default: this process), zombies left out by default."""
    root = os.getpid() if root is None else root
    table = _processes()
    children: dict[int, list[int]] = {}
    for pid, (parent, _, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found: list[int] = []
    queue = list(children.get(root, ()))
    while queue:
        pid = queue.pop()
        if pid in found or pid == root:
            continue
        found.append(pid)
        queue.extend(children.get(pid, ()))
    if alive_only:
        found = [p for p in found if table.get(p, (0, "Z", 0))[1] not in {"Z", "X"}]
    return sorted(found)


def _kill(pid: int) -> None:
    """SIGKILL one process through a pidfd where there is one, so a reused PID is safe."""
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            descriptor = os.pidfd_open(pid)
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_zombies() -> None:
    """Collect this process's exited children (reparented orphans included) by PID, so a
    child another part of the program waits for (a `Popen`) is never taken from it."""
    me = os.getpid()
    for pid, (parent, state, _) in _processes().items():
        if parent == me and state in {"Z", "X"}:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)


def clear(
    keep: frozenset[int] = frozenset(),
    group: int | None = None,
    code: str = "isolation_failed",
) -> int:
    """Kill every descendant of this process except `keep`; return how many were killed.

    `group`, when given, is also killed as a process group first (the only means off
    Linux). Raises `IsolationFailed(code)` if a descendant is still alive afterwards.
    """
    killed: set[int] = set()
    if group is not None:
        # Members of the step's group, wherever they were reparented (off Linux there is no
        # subreaper, so a group member that lost its parent is no longer a descendant).
        members = {
            pid
            for pid, (_, state, pgid) in _processes().items()
            if pgid == group and state not in {"Z", "X"} and pid not in keep
        }
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)
        killed |= members
    for _ in range(ROUNDS):
        strays = [p for p in descendants() if p not in keep]
        if not strays:
            _reap_zombies()
            return len(killed)
        for pid in strays:
            _kill(pid)
            killed.add(pid)
        time.sleep(PAUSE)
        _reap_zombies()
    survivors = [p for p in descendants() if p not in keep]
    if survivors:
        raise IsolationFailed(f"{len(survivors)} process(es) survived the step", code)
    return len(killed)


def _drain(stream: IO[bytes], sink: list[bytes]) -> None:
    kept = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        if kept < MAX_OUTPUT:
            sink.append(chunk[: MAX_OUTPUT - kept])
            kept += len(sink[-1])


def run_step(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    stdin: str | None = None,
    code: str = "isolation_failed",
) -> Step:
    """Run one untrusted command in its own session; leave no descendant behind."""
    keep = frozenset(descendants())
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        return Step(None, "", f"could not start: {type(error).__name__}", False, 0, 0)
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, err), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin.encode())
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    # Everything the step left behind, in its group or detached, before the pipes close.
    killed = clear(keep, group=process.pid, code=code)
    for reader in readers:
        reader.join(timeout=5)
    return Step(
        None if timed_out else process.returncode,
        b"".join(out).decode(errors="replace"),
        b"".join(err).decode(errors="replace"),
        timed_out,
        killed,
        int((time.monotonic() - started) * 1000),
    )
