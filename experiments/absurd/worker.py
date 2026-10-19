"""Pinned Absurd worker with fictional provider effects and fault barriers only."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from absurd_sdk import Absurd, CancelledTask, JsonValue, TaskContext


def connection(root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(root, "world.sqlite"), timeout=10)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def barrier(root: str, name: str, ctx: TaskContext | None = None) -> None:
    Path(root, name + ".entered").touch()
    Path(root, f"{name}.{os.getpid()}.entered").touch()
    deadline = time.monotonic() + 60
    while not (
        Path(root, name + ".release").exists()
        or Path(root, f"{name}.{os.getpid()}.release").exists()
    ):
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        if ctx is not None:
            try:
                ctx.heartbeat()
            except CancelledTask:
                Path(root, "heartbeat.cancelled").touch()
                raise
        time.sleep(0.05)


def output(root: str, value: int) -> int:
    with connection(root) as conn:
        conn.execute("INSERT INTO effects VALUES (?,?,?)", ("output", value, os.getpid()))
    return value


def permission_snapshot(root: str) -> bool:
    with connection(root) as conn:
        return bool(conn.execute("SELECT allowed FROM authority").fetchone()[0])


def effect(root: str, guard: str) -> str:
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


def retry_effect(root: str) -> str:
    attempted = Path(root, "attempted")
    if not attempted.exists():
        attempted.touch()
        barrier(root, "before_retry")
        raise RuntimeError("fictional failure before effect")
    return effect(root, "fresh")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--queue", default="default")
    args = parser.parse_args()
    app = Absurd(os.environ["FIT_DATABASE_URL"], queue_name=args.queue)

    @app.register_task(name="child" if args.queue == "children" else "fit")
    def task(params: Any, ctx: TaskContext) -> JsonValue:
        root, scenario, value = params["root"], params["scenario"], params["value"]
        try:
            saved = ctx.step("output", lambda: output(root, value))
            if scenario == "checkpoint":
                ctx.await_event("continue")
                return saved
            if scenario == "events":
                ctx.await_event("gate")
                first = ctx.await_event("input-one")
                second = ctx.await_event("input-two")
                return [first, second]
            if scenario == "repeat_event":
                first = ctx.await_event("input")
                second = ctx.await_event("input")
                return [first, second]
            if scenario == "timer":
                ctx.sleep_for("timer", 3)
                return "awake"
            if scenario == "child":
                child_id = ctx.step(
                    "spawn",
                    # psycopg returns UUID objects despite SpawnResult's str hint.
                    # Persist the standard UUID string, not an unserializable object.
                    lambda: str(
                        app.spawn(
                            "child",
                            {"root": root, "scenario": "checkpoint", "value": 2},
                            queue="children",
                            idempotency_key="fit-child",
                        )["task_id"]
                    ),
                )
                Path(root, "child-id.json").write_text(json.dumps(child_id))
                snapshot = ctx.await_task_result(child_id, queue_name="children")
                return {"child_state": snapshot.state, "result": snapshot.result}
            if scenario in {"auth_cached", "auth_fresh"}:
                approved = ctx.step("permission", lambda: permission_snapshot(root))
                ctx.await_event("continue")
                if not approved:
                    return "denied"
                return ctx.step(
                    "effect",
                    lambda: effect(
                        root,
                        "fresh" if scenario == "auth_fresh" else "cached",
                    ),
                )
            if scenario == "auth_retry":
                return ctx.step("effect", lambda: retry_effect(root))
            if scenario in {"cancel", "cancel_cooperative", "overlap_boundary"}:
                ctx.step(
                    "pause",
                    lambda: barrier(
                        root,
                        "pause",
                        ctx if scenario == "cancel_cooperative" else None,
                    ),
                )
                return ctx.step("effect", lambda: effect(root, "cached"))
            guards = {
                "external_raw": "none",
                "external_intent": "intent",
                "external_no_retry": "none",
                "overlap_inflight": "overlap",
                "overlap_fenced": "fenced",
                "cancel_inflight": "blocked",
            }
            return ctx.step("effect", lambda: effect(root, guards[scenario]))
        finally:
            Path(root, f"{os.getpid()}.handler_exited").touch()

    try:
        Path(args.root, f"{os.getpid()}.launched").touch()
        app.start_worker(
            worker_id=f"fit-worker-{os.getpid()}",
            claim_timeout=3,
            concurrency=1,
            batch_size=1,
            poll_interval=0.05,
        )
    finally:
        app.close()


if __name__ == "__main__":
    main()
