"""Keep the commit's code from touching the worker that reports on it (ADR-038).

The scenarios run as the worker's own uid, in its unit and PID namespace. Three guards keep
them from forging or disturbing the result:

1. **The worker is not dumpable** (`prctl(PR_SET_DUMPABLE, 0)`), set before anything is
   staged. A same-uid process can then open neither `/proc/<worker>/fd/*` nor
   `/proc/<worker>/mem`, and cannot ptrace it (the unit's seccomp filter also denies
   `@debug`, which holds `ptrace`). It must be a `prctl` call on the worker itself: no
   sandbox option sets it for a process that is already running, and `exec` resets it only
   for the new image, so `ctypes` against the C library is the smallest way.
2. **The worker is a child subreaper** (`PR_SET_CHILD_SUBREAPER`). A scenario's process that
   double-forks or calls `setsid` is reparented to the worker, never to the sandbox's init,
   so every process the commit starts stays in the worker's process tree. After each
   scenario, on its timeout, and before the report, the worker kills every descendant and
   reaps it, and checks that none is left. A process group is not enough (`setsid` leaves
   it) and the unit's cgroup holds the worker too, so the process tree under a subreaper is
   the boundary. A per-scenario sub-cgroup would need a delegated cgroup tree from the root
   helper; this needs nothing new from it.
3. **A scenario never has the worker's file descriptors**: its stdin is `/dev/null`, its
   output is a pipe, and `close_fds` is on.

A scenario can still kill the worker (signals between processes of one uid are allowed).
That makes the call fail with no result, never a forged one.

If any guard cannot be set, or a descendant survives the kill, the call is refused with
`env_isolation_failed` and reports nothing. Off Linux (portable tests on other platforms)
there is no `prctl` or `/proc`; the worker falls back to killing each scenario's process
group and reports `isolation: "process_group"`, which a host owner should never see.
"""

import contextlib
import ctypes
import os
import signal
import sys
import time
from pathlib import Path

from cardea.service.operations import OperationRejected

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
KILL_ROUNDS = 50


def failed(reason: str) -> OperationRejected:
    return OperationRejected(f"Scenario isolation failed: {reason}", "env_isolation_failed")


def linux() -> bool:
    return sys.platform.startswith("linux")


def harden() -> str:
    """Make this worker undumpable and a child subreaper; returns the isolation mode."""
    if not linux():
        return "process_group"
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.restype = ctypes.c_int
    zero = ctypes.c_ulong(0)
    if prctl(PR_SET_DUMPABLE, zero, zero, zero, zero) != 0:
        raise failed("the worker could not make itself non-dumpable")
    if prctl(PR_GET_DUMPABLE, zero, zero, zero, zero) != 0:
        raise failed("the worker is still dumpable")
    if prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(1), zero, zero, zero) != 0:
        raise failed("the worker could not become a child subreaper")
    flag = ctypes.c_int(0)
    if prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), zero, zero, zero) != 0 or not flag.value:
        raise failed("the worker is not a child subreaper")
    return "subreaper"


def _parents() -> dict[int, tuple[int, str]]:
    """Every visible process's parent and state, from `/proc/<pid>/stat`."""
    found: dict[int, tuple[int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # `pid (comm) state ppid ...`; comm may hold spaces and parentheses.
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 2:
            found[int(entry.name)] = (int(fields[1]), fields[0])
    return found


def descendants(alive_only: bool = True) -> list[int]:
    """This worker's descendants (with the subreaper, every process a scenario started)."""
    table = _parents()
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found, queue = [], list(children.get(os.getpid(), ()))
    while queue:
        pid = queue.pop()
        found.append(pid)
        queue.extend(children.get(pid, ()))
    if alive_only:
        found = [pid for pid in found if table.get(pid, (0, "Z"))[1] not in {"Z", "X"}]
    return found


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def clear(group: int | None = None) -> int:
    """Kill and reap every process the scenarios left; returns how many were killed.

    Raises `env_isolation_failed` if any survives (for example a fork loop faster than the
    kill, which the unit's task limit bounds).
    """
    if not linux():
        if group is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
        _reap()
        return 0
    killed: set[int] = set()
    for _ in range(KILL_ROUNDS):
        left = descendants()
        if not left:
            _reap()
            return len(killed)
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
        _reap()
    if descendants():
        raise failed("a process the scenario started survived")
    return len(killed)
