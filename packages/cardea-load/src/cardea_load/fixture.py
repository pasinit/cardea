"""Trusted disposable fixture adapter, run ONLY with the target API interpreter.

Reuses the frozen M1 SPIRE/service fixtures. The load package itself does not import
application modules. JSON IPC carries administration results; tokens stay in a
private rotating file, never responses, command arguments or report artifacts.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any


def main() -> None:
    repo, private = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path[:0] = [str(repo / p) for p in ("apps/api/src", "experiments/m1", "experiments/spire")]
    import rfc8785
    from cardea.kernel.models import ResourceRef  # type: ignore[import-not-found]
    from cardea.kernel.store import SQLiteKernelStore  # type: ignore[import-not-found]
    from harness import OUTSIDER, SUPERVISOR, ServiceLab, wait_for  # type: ignore[import-not-found]
    from provider_lab import Lab  # type: ignore[import-not-found]

    provider = Lab()
    service = None
    stopped = threading.Event()
    token_path = private / "credential.json"
    token_thread = None
    revoked_cursor = None

    def refresh() -> None:
        while not stopped.is_set():
            try:
                token = provider.token(SUPERVISOR, "cardea-kernel-fit")
                temporary = token_path.with_suffix(".new")
                fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump({"kind": "spiffe", "token": token}, stream)
                os.replace(temporary, token_path)
            except Exception:
                # Retain the last issued credential; the service still validates it.
                pass
            stopped.wait(5)

    def audit() -> dict[str, Any]:
        assert service is not None
        rows = []
        violations = []
        with sqlite3.connect(f"file:{service.data}/kernel.sqlite3?mode=ro", uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            bindings = conn.execute(
                "SELECT task_id,request_id,request_json,supervisor_id FROM task_bindings"
            ).fetchall()
            objects = dict(
                conn.execute("SELECT resource_id,count(*) FROM objects GROUP BY resource_id")
            )
            cursors = [r[0] for r in conn.execute("SELECT cursor FROM events ORDER BY cursor")]
        with SQLiteKernelStore(service.data) as store, store.transaction(write=False) as tx:
            for task_id, request_id, request_json, supervisor in bindings:
                task = tx.get_task(task_id)
                request = json.loads(request_json)
                row = {
                    "task_id": task_id,
                    "request_id": request_id,
                    "supervisor": supervisor,
                    "state": task.state.value,
                    "created_at": task.created_at.isoformat(),
                    "updated_at": task.updated_at.isoformat(),
                    "outputs": objects.get(task_id + ".output", 0),
                    "failure": task.failure.code if task.failure else None,
                    "verification": task.verification is not None,
                }
                try:
                    if row["outputs"]:
                        reference = ResourceRef(
                            namespace=request["namespace"], resource_id=task_id + ".output"
                        )
                        output = tx.latest_object(reference)
                        content = tx.read_object(output)
                        receipt = tx.get_operation(task_id, "publish")
                        row.update(
                            receipt_id=receipt.receipt_id,
                            receipt_sha256=hashlib.sha256(
                                receipt.model_dump_json().encode()
                            ).hexdigest(),
                        )
                        if content != rfc8785.dumps(request["payload"]) or row["outputs"] != 1:
                            violations.append(task_id + ":artifact_mismatch_or_duplicate")
                        if receipt.state.value != "succeeded":
                            violations.append(task_id + ":incomplete_receipt")
                        if task.state.value == "succeeded":
                            assert task.verification is not None and task.verification.passed
                            assert (
                                task.checkpoint is not None
                                and task.checkpoint.input_versions == (output,)
                            )
                            proof = tx.latest_object(
                                ResourceRef(
                                    namespace=request["namespace"],
                                    resource_id=task_id + ".verification",
                                )
                            )
                            assert json.loads(tx.read_object(proof)) == {
                                "passed": True,
                                "output_digest": output.content_digest,
                            }
                    elif task.state.value == "succeeded":
                        violations.append(task_id + ":false_success_without_output")
                    if supervisor != "supervisor":
                        violations.append(task_id + ":wrong_supervisor")
                except Exception as error:
                    violations.append(task_id + ":" + type(error).__name__)
                rows.append(row)
        if integrity != "ok" or foreign_keys:
            violations.append("database_integrity")
        if cursors != sorted(set(cursors)):
            violations.append("event_cursor_order")
        return {
            "tasks": rows,
            "violations": violations,
            "integrity": integrity,
            "foreign_key_violations": len(foreign_keys),
            "events": len(cursors),
        }

    try:
        for line in sys.stdin:
            try:
                command = json.loads(line)
                with contextlib.redirect_stdout(sys.stderr):
                    action = command["action"]
                    if action == "boot":
                        provider.start()
                        provider.register(SUPERVISOR, ttl=120)
                        provider.register(OUTSIDER)
                        wait_for(lambda: provider.token(SUPERVISOR))
                        token_thread = threading.Thread(target=refresh, daemon=True)
                        token_thread.start()
                        wait_for(lambda: token_path.read_text())
                        response = {
                            "ready": True,
                            "provider_pids": [p.pid for p in provider.processes],
                        }
                    elif action == "reset":
                        if service:
                            service.close()
                        service = ServiceLab(provider)
                        process = service.start(command.get("barrier", ""))
                        response = {
                            "socket": str(service.data / "kernel.sock"),
                            "database": str(service.data / "kernel.sqlite3"),
                            "pid": process.pid,
                            "credential": str(token_path),
                        }
                    elif action == "audit":
                        response = audit()
                    elif action == "counts":
                        assert service
                        with sqlite3.connect(
                            f"file:{service.data}/kernel.sqlite3?mode=ro", uri=True
                        ) as conn:
                            response = dict(
                                conn.execute("SELECT state,count(*) FROM tasks GROUP BY state")
                            )
                    elif action == "crash":
                        assert service
                        service.stop(service.processes[-1])
                        response = {"stopped": True}
                    elif action == "restart":
                        assert service
                        response = {"pid": service.start().pid}
                    elif action == "revoke":
                        assert service
                        document = service.document.model_copy(
                            update={
                                "permissions": tuple(
                                    p
                                    for p in service.document.permissions
                                    if not (
                                        p.principal_id == "supervisor"
                                        and p.operation == "objects.write"
                                    )
                                )
                            }
                        )
                        service.apply(document)
                        with sqlite3.connect(
                            f"file:{service.data}/kernel.sqlite3?mode=ro", uri=True
                        ) as conn:
                            revoked_cursor = conn.execute(
                                "SELECT MAX(cursor) FROM events WHERE event_type='policy.updated'"
                            ).fetchone()[0]
                        response = {
                            "committed": True,
                            "at_ns": time.time_ns(),
                            "policy_cursor": revoked_cursor,
                        }
                    elif action == "after_revoke":
                        assert service and revoked_cursor is not None
                        with sqlite3.connect(
                            f"file:{service.data}/kernel.sqlite3?mode=ro", uri=True
                        ) as conn:
                            writes = conn.execute(
                                "SELECT count(*) FROM events "
                                "WHERE event_type='object.written' AND cursor > ?",
                                (revoked_cursor,),
                            ).fetchone()[0]
                        response = {"writes": writes}
                    elif action == "provider_stop":
                        provider.stop(provider.agent)
                        response = {"stopped": True}
                    elif action == "provider_restart":
                        provider.start_agent()
                        wait_for(lambda: provider.token(SUPERVISOR))
                        response = {"ready": True}
                    elif action == "close":
                        break
                    else:
                        raise ValueError("Unknown fixture action")
                print(json.dumps({"ok": response}), flush=True)
            except Exception as error:
                import traceback

                traceback.print_exc(file=sys.stderr)
                print(json.dumps({"error": type(error).__name__}), flush=True)
    finally:
        stopped.set()
        if token_thread:
            token_thread.join(timeout=5)
        if service:
            service.close()
        provider.close()


if __name__ == "__main__":
    main()
