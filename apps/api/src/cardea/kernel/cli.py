"""Local store administration and persistence demo, not an authenticated agent transport."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from cardea.kernel.demo import resume_demo, start_demo
from cardea.kernel.lifecycle import ContractError
from cardea.kernel.models import ResourceRef, TaskState
from cardea.kernel.store import SQLiteKernelStore, StoreError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".cardea"))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("demo-start")
    start.add_argument("--task-id", default=f"demo-{uuid4().hex}")
    for name in ("demo-resume", "inspect", "cancel", "recover"):
        commands.add_parser(name).add_argument("task_id")
    events = commands.add_parser("events")
    events.add_argument("--task-id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    objects = commands.add_parser("object")
    objects.add_argument("namespace")
    objects.add_argument("resource_id")
    args = parser.parse_args(argv)
    try:
        with SQLiteKernelStore(args.directory) as store:
            now = datetime.now(UTC)
            result: object
            if args.command == "demo-start":
                result = start_demo(store, task_id=args.task_id, at=now).model_dump(mode="json")
            elif args.command == "demo-resume":
                result = resume_demo(store, task_id=args.task_id, at=now).model_dump(mode="json")
            else:
                with store.transaction(write=args.command in {"cancel", "recover"}) as transaction:
                    if args.command == "events":
                        result = [
                            event.model_dump(mode="json")
                            for event in transaction.read_events(
                                task_id=args.task_id,
                                after_cursor=args.after,
                                limit=args.limit,
                            )
                        ]
                    elif args.command == "object":
                        reference = transaction.latest_object(
                            ResourceRef(
                                namespace=args.namespace,
                                resource_id=args.resource_id,
                            )
                        )
                        result = {
                            "reference": reference.model_dump(mode="json"),
                            "content_utf8": transaction.read_object(reference).decode("utf-8"),
                        }
                    else:
                        task = transaction.get_task(args.task_id)
                        if args.command == "cancel":
                            task = transaction.change_task(
                                task.task_id,
                                TaskState.CANCELLED,
                                expected_revision=task.revision,
                                at=now,
                                lease_id=task.lease.lease_id if task.lease else None,
                            )
                        elif args.command == "recover":
                            task = transaction.recover_task(
                                task.task_id,
                                expected_revision=task.revision,
                                at=now,
                            )
                        result = task.model_dump(mode="json")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        StoreError,
        ContractError,
        ValidationError,
        sqlite3.Error,
        OSError,
        UnicodeError,
    ) as error:
        code = (
            str(error.code) if isinstance(error, (StoreError, ContractError)) else "invalid_request"
        )
        # Validation errors may contain input data; keep CLI error output bounded and generic.
        message = (
            str(error) if isinstance(error, (StoreError, ContractError)) else "Request failed."
        )
        print(json.dumps({"error": {"code": code, "message": message}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
