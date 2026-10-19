"""Fictional provider and fault barriers, not Cardea broker implementations."""

import os
import sqlite3
import time
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import CancelledError


def connection(root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(root, "world.sqlite"), timeout=10)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def barrier(root: str, name: str, *, heartbeat: bool = False) -> None:
    Path(root, name + ".entered").touch()
    Path(root, f"{name}.{os.getpid()}.{activity.info().attempt}.entered").touch()
    deadline = time.monotonic() + 60
    while not (
        Path(root, name + ".release").exists()
        or Path(root, f"{name}.{os.getpid()}.release").exists()
    ):
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        if heartbeat:
            activity.heartbeat()
        time.sleep(0.02)


@activity.defn
def output(root: str, value: int) -> int:
    with connection(root) as conn:
        conn.execute("INSERT INTO effects VALUES (?,?,?)", ("output", value, os.getpid()))
    return value


@activity.defn
def permission_snapshot(root: str) -> bool:
    with connection(root) as conn:
        return bool(conn.execute("SELECT allowed FROM authority").fetchone()[0])


@activity.defn
def pause(root: str, cooperative: bool) -> None:
    try:
        barrier(root, "pause", heartbeat=cooperative)
    except CancelledError:
        Path(root, "pause.cancelled").touch()
        raise
    finally:
        Path(root, "pause.exited").touch()


@activity.defn
def external_effect(root: str, guard: str) -> str:
    try:
        return perform_effect(root, guard)
    except CancelledError:
        Path(root, f"{os.getpid()}.cancelled").touch()
        raise


@activity.defn(no_thread_cancel_exception=True)
def uninterruptible_effect(root: str, guard: str) -> str:
    # Explicit SDK option, NOT default behavior or proof of remote I/O semantics.
    return perform_effect(root, guard)


def perform_effect(root: str, guard: str) -> str:
    if guard in {"overlap", "fenced", "blocked"}:
        barrier(root, "inflight")
    with connection(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if guard == "fenced":
            current = conn.execute("SELECT generation FROM authority").fetchone()[0]
            if current != int(os.environ["FIT_GENERATION"]):
                Path(root, f"{os.getpid()}.denied").touch()
                return "stale_owner"
        if guard == "fresh" and not conn.execute("SELECT allowed FROM authority").fetchone()[0]:
            return "denied"
        if guard == "intent":
            if conn.execute("SELECT 1 FROM intents WHERE id=1").fetchone():
                return "outcome_unknown"
            conn.execute("INSERT INTO intents VALUES (1)")
            conn.commit()
        conn.execute("INSERT INTO effects VALUES (?,?,?)", ("external", 1, os.getpid()))
    Path(root, f"{os.getpid()}.effect").touch()
    if guard in {"none", "intent"}:
        barrier(root, "effect_committed")
    return "effect_recorded"


@activity.defn
def retry_effect(root: str) -> str:
    if activity.info().attempt == 1:
        barrier(root, "before_retry")
        raise RuntimeError("fictional transient failure before effect")
    return external_effect(root, "fresh")
