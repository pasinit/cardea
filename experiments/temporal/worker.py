"""One disposable Temporal worker process; no workflow submission or Cardea service."""

import argparse
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    external_effect,
    output,
    pause,
    permission_snapshot,
    retry_effect,
    uninterruptible_effect,
)
from workflows import FitWorkflow


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("queue")
    parser.add_argument("root")
    args = parser.parse_args()
    client = await Client.connect(args.address, identity=f"fit-worker-{os.getpid()}")
    with ThreadPoolExecutor(max_workers=8) as executor:
        worker = Worker(
            client,
            task_queue=args.queue,
            workflows=[FitWorkflow],
            activities=[
                output,
                pause,
                permission_snapshot,
                external_effect,
                retry_effect,
                uninterruptible_effect,
            ],
            activity_executor=executor,
            # One activity slot prevents the suspended worker from reserving its
            # own replacement attempt through an outstanding long poll.
            max_concurrent_activities=1,
            max_concurrent_activity_task_polls=1,
            max_concurrent_workflow_task_polls=2,
            sticky_queue_schedule_to_start_timeout=timedelta(seconds=1),
            max_heartbeat_throttle_interval=timedelta(milliseconds=100),
        )
        async with worker:
            Path(args.root, f"{os.getpid()}.launched").touch()
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
