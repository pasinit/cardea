"""Bounded supervisor-side snapshot ingestion; no worker receives a host path."""

import os
import stat
from hashlib import sha256
from pathlib import Path

import rfc8785

from cardea.runtime.incidents import Incident, IncidentBatch
from cardea.service.client import KernelClient
from cardea.service.models import AgentInput, TaskView


def snapshot(directory: Path) -> IncidentBatch:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
        if not 1 <= len(names) <= 20:
            raise ValueError("Input directory must contain 1 to 20 JSON records")
        records = []
        for name in names:
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            try:
                before = os.fstat(child)
                if not stat.S_ISREG(before.st_mode) or before.st_size > 8192:
                    raise ValueError("Input must be a bounded regular file")
                data = os.read(child, 8193)
                after = os.fstat(child)
                if len(data) > 8192 or (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("Input changed during ingestion; rescan")
                records.append(Incident.model_validate_json(data))
            finally:
                os.close(child)
        return IncidentBatch(records=records)
    finally:
        os.close(descriptor)


def deliver(client: KernelClient, task_id: str, directory: Path) -> TaskView:
    batch = snapshot(directory)
    digest = sha256(rfc8785.dumps(batch.model_dump())).hexdigest()
    return client.append_input(task_id, AgentInput(request_id=f"input-{digest}", inputs=batch))
