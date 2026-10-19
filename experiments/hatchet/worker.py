"""Public Hatchet tasks and fictional effect boundaries; no Cardea integration."""

import os
import sqlite3
import time
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from typing import Any

from hatchet_sdk import Context, DurableContext
from hatchet_sdk.types.idempotency import TTLBasedIdempotencyConfig

from runtime import Input, client

hatchet = client(Path(os.environ["FIT_CONNECTION"]), os.environ["FIT_NAMESPACE"])


def connection(root: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(root, "world.sqlite"), timeout=10)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def barrier(root: str, name: str) -> None:
    Path(root, name + ".entered").touch()
    deadline = time.monotonic() + 90
    while not Path(root, name + ".release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(0.05)


@hatchet.task(name="output", input_validator=Input)
def output(data: Input, ctx: Context) -> dict[str, int]:
    with connection(data.root) as conn:
        conn.execute("INSERT INTO effects VALUES (?,?)", ("output", data.value))
    return {"value": data.value}


@hatchet.task(name="permission", input_validator=Input)
def permission(data: Input, ctx: Context) -> dict[str, bool]:
    with connection(data.root) as conn:
        return {"allowed": bool(conn.execute("SELECT allowed FROM authority").fetchone()[0])}


@hatchet.task(name="effect", input_validator=Input, retries=1)
def effect(data: Input, ctx: Context) -> dict[str, str]:
    try:
        if data.scenario == "auth_retry" and ctx.retry_count == 0:
            barrier(data.root, "before_retry")
            raise RuntimeError("fictional failure before effect")
        if data.scenario == "cancel_inflight":
            barrier(data.root, "inflight")
        with connection(data.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if (
                data.scenario in {"auth_fresh", "auth_retry"}
                and not conn.execute("SELECT allowed FROM authority").fetchone()[0]
            ):
                return {"status": "denied"}
            if data.scenario == "external_intent":
                if conn.execute("SELECT 1 FROM intents WHERE id=1").fetchone():
                    return {"status": "outcome_unknown"}
                conn.execute("INSERT INTO intents VALUES (1)")
                conn.commit()
            conn.execute("INSERT INTO effects VALUES (?,?)", ("external", 1))
        if data.scenario.startswith("external") and ctx.retry_count == 0:
            barrier(data.root, "effect_committed")
            raise RuntimeError("fictional lost effect result")
        return {"status": "effect_recorded"}
    finally:
        Path(data.root, "effect.exited").touch()


@hatchet.durable_task(
    name="fit",
    input_validator=Input,
    retries=1,
    idempotency=TTLBasedIdempotencyConfig(
        key_expression="input.operation_key", ttl=timedelta(minutes=10)
    ),
)
async def task(data: Input, ctx: DurableContext) -> dict[str, Any]:
    saved = await output.aio_run(data, child_key="output")
    if data.scenario == "smoke":
        return saved
    if data.scenario.startswith("auth_") and data.scenario != "auth_retry":
        approved = await permission.aio_run(data, child_key="permission")
        Path(data.root, "permission.saved").touch()
        await ctx.aio_wait_for_event(
            "continue", scope=data.scope, lookback_window=timedelta(minutes=5)
        )
        if not approved["allowed"]:
            return {"status": "denied"}
    elif data.scenario in {"checkpoint", "events", "repeat_event"}:
        Path(data.root, "checkpoint.saved").touch()
        first = await ctx.aio_wait_for_event(
            "continue", scope=data.scope, lookback_window=timedelta(minutes=5)
        )
        if data.scenario == "checkpoint":
            return saved
        second = await ctx.aio_wait_for_event(
            "continue" if data.scenario == "repeat_event" else "second",
            scope=data.scope,
            lookback_window=timedelta(minutes=5),
        )
        return {"events": [first, second]}
    elif data.scenario == "timer":
        Path(data.root, "timer.entered").touch()
        await ctx.aio_sleep_for(timedelta(seconds=4))
        return {"status": "awake"}
    return await effect.aio_run(data, child_key="effect")


async def lifespan() -> AsyncGenerator[None, None]:
    Path(os.environ["FIT_ROOT"], "worker.ready").touch()
    yield None


if __name__ == "__main__":
    worker = hatchet.worker(
        "worker-" + str(os.getpid()),
        slots=4,
        durable_slots=4,
        workflows=[task, output, permission, effect],
        lifespan=lifespan,
    )
    worker.start()
