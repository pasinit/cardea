"""Native SDK persistence fit; records behavior, not Cardea guarantees."""

import asyncio
from pathlib import Path

from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import a2a_pb2 as a2a
from sqlalchemy.ext.asyncio import create_async_engine


def test_sql_store_restart_and_owner_scope(tmp_path: Path) -> None:
    async def exercise() -> None:
        url = f"sqlite+aiosqlite:///{tmp_path / 'a2a.sqlite3'}"
        context = ServerCallContext(state={"owner": "alice"})
        engine = create_async_engine(url)
        store = DatabaseTaskStore(engine, owner_resolver=lambda c: c.state["owner"])
        task = a2a.Task(
            id="peer-task",
            context_id="conversation",
            status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING),
            history=[
                a2a.Message(
                    message_id="message-1",
                    role=a2a.Role.ROLE_USER,
                    parts=[a2a.Part(text="bounded objective")],
                )
            ],
        )
        await store.save(task, context)
        await engine.dispose()
        engine = create_async_engine(url)
        restored = DatabaseTaskStore(engine, owner_resolver=lambda c: c.state["owner"])
        assert await restored.get(task.id, context) == task
        assert await restored.get(task.id, ServerCallContext(state={"owner": "bob"})) is None
        # The preserved working record is data, with no execution checkpoint/scheduler.
        again = await restored.get(task.id, context)
        assert again is not None
        assert again.status.state == a2a.TaskState.TASK_STATE_WORKING
        await engine.dispose()

    asyncio.run(exercise())


def test_store_save_does_not_enforce_execution_terminality(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a2a.sqlite3'}")
        store = DatabaseTaskStore(engine, owner_resolver=lambda c: "alice")
        context = ServerCallContext()
        done = a2a.Task(
            id="peer-task",
            status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_COMPLETED),
        )
        await store.save(done, context)
        stale = a2a.Task(
            id="peer-task",
            status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING),
        )
        await store.save(stale, context)
        again = await store.get(done.id, context)
        assert again is not None
        assert again.status.state == a2a.TaskState.TASK_STATE_WORKING
        await engine.dispose()

    asyncio.run(exercise())
