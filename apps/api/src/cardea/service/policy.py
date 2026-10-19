"""Casbin decisions from the current SQLite transaction, with no authorization cache."""

import json
import time

import casbin  # type: ignore[import-untyped]
import rfc8785
import sqlalchemy as sa
from casbin.model import Model  # type: ignore[import-untyped]

from cardea.kernel.models import ObjectVersion
from cardea.service.database import Fault
from cardea.service.models import HostObjectTaskRequest, TaskRequest

MODEL = """
[request_definition]
r = sub, obj, act, mode
[policy_definition]
p = sub, obj, act, mode
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = r.sub == p.sub && r.obj == p.obj && r.act == p.act && r.mode == p.mode
"""


def object_key(reference: ObjectVersion) -> str:
    """A disjoint Casbin object key; namespace identifiers cannot contain JSON syntax."""
    return rfc8785.dumps(
        [
            "object-version",
            reference.namespace,
            reference.resource_id,
            reference.version,
            reference.content_digest,
        ]
    ).decode()


def recovery_prefix(conn: sa.Connection) -> str | None:
    row = conn.exec_driver_sql(
        "SELECT epoch FROM recovery_epochs ORDER BY rowid DESC LIMIT 1"
    ).first()
    return f"r{row[0]}." if row else None


class Policy:
    def __init__(self, conn: sa.Connection) -> None:
        self.conn = conn
        self.recovery_prefix = recovery_prefix(conn)
        model = Model()
        model.load_model_from_text(MODEL)
        self.enforcer = casbin.Enforcer(model)
        for row in conn.exec_driver_sql(
            "SELECT principal_id,namespace,operation,mode FROM policies WHERE expires_at > ?",
            (time.time(),),
        ):
            self.enforcer.add_policy(*row)

    def principal(self, principal: str) -> sa.RowMapping:
        row = (
            self.conn.exec_driver_sql("SELECT * FROM principals WHERE principal_id=?", (principal,))
            .mappings()
            .first()
        )
        if row is None or row["disabled"]:
            raise Fault("principal_disabled", "Principal is unknown or disabled")
        return row

    def require(self, principal: str, namespace: str, operation: str, mode: str = "allow") -> None:
        if (
            self.recovery_prefix
            and operation not in {"tasks.inspect", "objects.read", "events.read"}
            and not namespace.startswith(self.recovery_prefix)
        ):
            raise Fault(
                "restored_scope_sealed", "New execution requires a fresh recovery namespace"
            )
        self._current_authority(principal, namespace, operation, mode)

    def _current_authority(
        self, principal: str, namespace: str, operation: str, mode: str = "allow"
    ) -> None:
        if principal != "platform":
            self.principal(principal)
        if not self.enforcer.enforce(principal, namespace, operation, mode):
            raise Fault("authority_denied", "Current authority does not permit this operation")

    def binding(self, task_id: str) -> sa.RowMapping:
        row = (
            self.conn.exec_driver_sql("SELECT * FROM task_bindings WHERE task_id=?", (task_id,))
            .mappings()
            .first()
        )
        if row is None:
            raise Fault("task_not_found", "No accessible authenticated task", 404)
        return row

    def ceiling(self, task_id: str, operation: str) -> sa.RowMapping:
        first = self.binding(task_id)
        current: sa.RowMapping | None = first
        visited: set[str] = set()
        while current is not None:
            identity = str(current["task_id"])
            if identity in visited or len(visited) >= 32:
                raise Fault("invalid_provenance", "Task provenance cannot be established")
            visited.add(identity)
            for name in ("agent_id", "runtime_id", "supervisor_id"):
                self.principal(str(current[name]))
            if operation not in json.loads(current["grants_json"]):
                raise Fault("task_grant_denied", "Operation is outside explicit task grants")
            namespace = str(current["namespace"])
            self.require("platform", namespace, operation)
            self.require(str(current["supervisor_id"]), namespace, operation)
            self.require(str(current["supervisor_id"]), namespace, operation, "delegate")
            if identity != task_id and "tasks.delegate" in json.loads(current["grants_json"]):
                # Historical descendant reads still check live delegation authority,
                # without authorizing any new delegation in a sealed namespace.
                self._current_authority("platform", namespace, "tasks.delegate")
                self._current_authority(str(current["supervisor_id"]), namespace, "tasks.delegate")
                self._current_authority(
                    str(current["supervisor_id"]), namespace, "tasks.delegate", "delegate"
                )
            parent = current["parent_task_id"]
            current = self.binding(str(parent)) if parent else None
        return first

    def access(self, principal: str, task_id: str, operation: str) -> sa.RowMapping:
        self.principal(principal)
        binding = self.binding(task_id)
        if principal not in {binding["supervisor_id"], binding["agent_id"], binding["runtime_id"]}:
            raise Fault("task_not_found", "No accessible authenticated task", 404)
        return self.ceiling(task_id, operation)

    def admission(self, supervisor: str, request: TaskRequest, runtime: str) -> None:
        self.require(supervisor, request.namespace, "tasks.submit")
        self.require("platform", request.namespace, "tasks.submit")
        self.principal(runtime)
        agent = self.principal(request.agent_id)
        if agent["kind"] != "agent" or agent["runtime_id"] != runtime:
            raise Fault("runtime_binding", "Agent is not assigned to the installed runtime")
        if request.parent_task_id:
            parent = self.binding(request.parent_task_id)
            if parent["supervisor_id"] != supervisor or parent["namespace"] != request.namespace:
                raise Fault("parent_binding", "Child must retain its supervisor and namespace")
            self.ceiling(request.parent_task_id, "tasks.submit")
        for operation in request.grants:
            self.require("platform", request.namespace, operation)
            self.require(supervisor, request.namespace, operation)
            self.require(supervisor, request.namespace, operation, "delegate")
            if request.parent_task_id:
                self.ceiling(request.parent_task_id, operation)
        if isinstance(request, HostObjectTaskRequest):
            self.object_read(supervisor, request.input_object)

    def object_read(self, supervisor: str, reference: ObjectVersion) -> float:
        """Require both namespace and exact-version ceilings; return their earliest expiry."""
        keys = (reference.namespace, object_key(reference))
        for key in keys:
            self.require(supervisor, key, "objects.read")
            self.require(supervisor, key, "objects.read", "delegate")
            self.require("platform", key, "objects.read")
        return float(
            self.conn.exec_driver_sql(
                "SELECT MIN(expires_at) FROM policies WHERE namespace IN (?,?) "
                "AND operation='objects.read' "
                "AND ((principal_id=? AND mode IN ('allow','delegate')) "
                "OR (principal_id='platform' AND mode='allow'))",
                (*keys, supervisor),
            ).scalar_one()
        )
