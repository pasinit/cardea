"""A member's own memory (owner decision 2026-11-24, with the ADR-037 workspace).

An agent with a workspace keeps notes of its past work in `./memory/`. It is a folder of
files it organises itself, with `INDEX.md` as the short summary that goes into every
prompt. `cardea-memory save` uploads the folder as a gzipped tar to the run's loopback
endpoint. The kernel stores it as a new version of one object per member,
`memory.<agent_id>`, in the run's namespace, fenced by the attempt's lease. The next run
with a workspace gets the latest version unpacked into `./memory/` before its session
starts.

The archive is checked here before it is stored: regular files and directories only,
relative paths without `..`, bounded in size and count. The memory is the member's notes,
not instructions; every version is kept with its producer, so a bad lesson can be read
and rolled back.
"""

import io
import tarfile
from typing import Any

from cardea.kernel.models import ResourceRef
from cardea.kernel.store import MAX_OBJECT_BYTES, StoreError
from cardea.service.database import Database, borrow
from cardea.service.models import now

# The archive is one store object (1 MiB); unpacked, it may hold more text than that.
MAX_ARCHIVE_BYTES = MAX_OBJECT_BYTES
MAX_UNPACKED_BYTES = 4 * 1048576
MAX_FILES = 512


class MemoryRefused(Exception):
    """The upload is not an acceptable memory archive; nothing was stored."""


def reference(namespace: str, agent_id: str) -> ResourceRef:
    return ResourceRef(namespace=namespace, resource_id=f"memory.{agent_id}")


def check(content: bytes) -> int:
    """Validate a memory archive; returns its unpacked size."""
    if not content or len(content) > MAX_ARCHIVE_BYTES:
        raise MemoryRefused(f"a memory archive holds 1 to {MAX_ARCHIVE_BYTES} bytes")
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
    except (tarfile.TarError, OSError, EOFError):
        raise MemoryRefused("a memory archive is a gzipped tar") from None
    if len(members) > MAX_FILES:
        raise MemoryRefused(f"a memory archive holds at most {MAX_FILES} entries")
    total = 0
    for member in members:
        name = member.name
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or not (member.isfile() or member.isdir()):
            raise MemoryRefused(f"memory entry {name[:80]!r} is not a plain relative file")
        total += member.size
    if total > MAX_UNPACKED_BYTES:
        raise MemoryRefused(f"a memory unpacks to at most {MAX_UNPACKED_BYTES} bytes")
    return total


def save(
    database: Database,
    namespace: str,
    agent_id: str,
    task_id: str,
    lease_id: str,
    content: bytes,
) -> dict[str, Any]:
    """Store a new version of the member's memory under the attempt's lease."""
    unpacked = check(content)
    reference_ = reference(namespace, agent_id)
    with database.engine.begin() as conn, borrow(conn) as tx:
        try:
            current = tx.latest_object(reference_).version
        except StoreError:
            current = 0
        version = tx.put_object(
            reference_,
            content,
            expected_version=current,
            producer_id=agent_id,
            task_id=task_id,
            lease_id=lease_id,
            at=now(),
        )
    return {"version": version.version, "bytes": len(content), "unpacked_bytes": unpacked}


def latest(database: Database, namespace: str, agent_id: str) -> bytes | None:
    """The member's newest memory archive, or None before its first save."""
    with database.read() as conn, borrow(conn) as tx:
        try:
            version = tx.latest_object(reference(namespace, agent_id))
        except StoreError:
            return None
        return tx.read_object(version)
