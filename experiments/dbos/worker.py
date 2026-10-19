"""Fictional workflows and fault barriers; no Cardea production integration."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

from dbos import DBOS, SetWorkflowID


def mark(root: str, name: str) -> None:
    Path(root, name).touch()


def barrier(root: str, name: str) -> None:
    mark(root, name + ".entered")
    deadline = time.monotonic() + 90
    while not Path(root, name + ".release").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        time.sleep(0.02)


def connection(root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(root, "world.sqlite"), timeout=10)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@DBOS.step()
def output(root: str, value: int) -> int:
    with connection(root) as conn:
        conn.execute(
            "INSERT INTO effects(kind,value,worker) VALUES (?,?,?)", ("output", value, os.getpid())
        )
    return value


@DBOS.step()
def pause(root: str, name: str) -> None:
    barrier(root, name)


@DBOS.step()
def permission_snapshot(root: str) -> bool:
    with connection(root) as conn:
        return bool(conn.execute("SELECT allowed FROM authority").fetchone()[0])


@DBOS.step()
def external_effect(root: str, guard: str) -> str:
    # This fake external system is a different database from the workflow engine.
    # 'intent' is an intentionally tiny fixture, not a proposed production broker.
    if guard in {"overlap", "overlap_fenced", "blocked"}:
        barrier(root, "inflight")
    with connection(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if guard == "overlap_fenced":
            current = conn.execute("SELECT generation FROM authority").fetchone()[0]
            if current != int(os.environ["FIT_GENERATION"]):
                return "stale_owner"
        if guard == "fresh" and not conn.execute("SELECT allowed FROM authority").fetchone()[0]:
            return "denied"
        if guard == "intent":
            if conn.execute("SELECT 1 FROM intents WHERE id=1").fetchone():
                return "outcome_unknown"
            conn.execute("INSERT INTO intents VALUES (1)")
            conn.commit()  # Intent survives even if the external result is lost.
        conn.execute(
            "INSERT INTO effects(kind,value,worker) VALUES (?,?,?)", ("external", 1, os.getpid())
        )
    if guard in {"none", "intent"}:
        barrier(root, "effect_committed")
    return "effect_recorded"


@DBOS.step(retries_allowed=True, interval_seconds=0.2, max_attempts=2)
def retry_effect(root: str) -> str:
    attempt = Path(root, "attempted")
    if not attempt.exists():
        attempt.touch()
        barrier(root, "before_retry")
        raise RuntimeError("fictional transient failure before effect")
    # Deliberately inside each attempt, not a separately cached workflow step.
    with connection(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT allowed FROM authority").fetchone()[0]:
            return "denied"
        conn.execute(
            "INSERT INTO effects(kind,value,worker) VALUES (?,?,?)", ("external", 1, os.getpid())
        )
    return "effect_recorded"


@DBOS.workflow()
def child(root: str) -> int:
    saved = output(root, 2)
    pause(root, "child")
    return saved


@DBOS.workflow()
def task(root: str, scenario: str, value: int = 1) -> object:
    saved = output(root, value)
    if scenario == "checkpoint":
        pause(root, "checkpoint")
        return saved
    if scenario == "events":
        pause(root, "before_recv")
        DBOS.set_event("receiving", True)
        first = DBOS.recv("input", timeout_seconds=20)
        second = DBOS.recv("input", timeout_seconds=20)
        return [first, second]
    if scenario == "child":
        return DBOS.start_workflow(child, root).get_result()
    if scenario == "timer":
        DBOS.set_event("sleeping", True)
        DBOS.sleep(3)
        return "awake"
    if scenario in {"external_raw", "external_intent"}:
        return external_effect(root, "none" if scenario == "external_raw" else "intent")
    if scenario in {"auth_cached", "auth_fresh"}:
        approved = permission_snapshot(root)
        DBOS.set_event("waiting_for_authority_test", True)
        DBOS.recv("continue", timeout_seconds=30)
        if not approved:
            return "denied"
        return external_effect(root, "cached" if scenario == "auth_cached" else "fresh")
    if scenario == "auth_retry":
        return retry_effect(root)
    if scenario in {"overlap_inflight", "overlap_fenced", "cancel_inflight"}:
        guard = {
            "overlap_inflight": "overlap",
            "overlap_fenced": "overlap_fenced",
            "cancel_inflight": "blocked",
        }[scenario]
        return external_effect(root, guard)
    if scenario == "overlap":
        pause(root, "overlap")
        return external_effect(root, "cached")
    if scenario == "cancel":
        pause(root, "cancel")
        return external_effect(root, "cached")
    raise ValueError(scenario)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["start", "recover"])
    parser.add_argument("root")
    parser.add_argument("scenario")
    parser.add_argument("--value", type=int, default=1)
    args = parser.parse_args()
    DBOS(
        config={
            "name": "cardea-dbos-fit",
            "system_database_url": os.environ["FIT_DATABASE_URL"],
            "application_version": "fit-v1",
            "executor_id": "fit-worker",
            "enable_otlp": False,
            "log_level": "WARNING",
            "use_listen_notify": False,
            "notification_listener_polling_interval_sec": 0.05,
        }
    )
    DBOS.launch()
    mark(args.root, f"{os.getpid()}.launched")
    try:
        if args.mode == "start":
            with SetWorkflowID("fit-task"):
                handle = DBOS.start_workflow(task, args.root, args.scenario, args.value)
        else:
            # Public startup recovery for the same executor identity; no private API.
            handle = DBOS.retrieve_workflow("fit-task")
        result = handle.get_result()
        Path(args.root, f"{os.getpid()}.result.json").write_text(json.dumps(result))
    finally:
        DBOS.destroy()


if __name__ == "__main__":
    main()
