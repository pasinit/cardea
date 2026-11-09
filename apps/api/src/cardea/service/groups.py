"""Agent groups (ADR-034 decision 1, implementation slice 4).

A supervisor applies an `AgentGroup` manifest. In one SQLite transaction the kernel:

- checks the caller is `spec.supervisor` and holds `groups.manage` (and platform policy);
- creates each member principal `<group>.<pool>.<n>`, runtime-bound with no external
  identity, and nothing else;
- checks every pool grant against the supervisor's live, delegable authority, naming the
  pool and grant that fail;
- compiles each pool's run template and time triggers into one ADR-029 trigger per member
  per manifest trigger (`managed_by = 'group'`), each passing full admission now.

Every slot still runs full admission (`Kernel._admit`), so a later revocation or disabled
supervisor stops the members. A member runs one task at a time: a slot that arrives while
its previous run is unfinished is coalesced (recorded, not admitted).

`?dry_run=true` runs the same transaction and rolls it back, so its plan and its refusals
are exactly those of the real apply.

Slice 4 converges synchronously inside the apply transaction (create, update, scale-up),
so `observedGeneration` equals `generation` on commit. Scale-down, pool removal and group
deletion drain members; that is slice 8 and is refused until then.
"""

import json
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, NoReturn

import rfc8785
import sqlalchemy as sa
from pydantic import JsonValue, ValidationError

from cardea.service import mailbox
from cardea.service.auth import Proof
from cardea.service.database import Fault, borrow
from cardea.service.models import (
    MAX_POOL_TRIGGERS,
    AgentGroup,
    AgentRunRequest,
    GroupPool,
    GroupSpec,
    TaskRequest,
    TriggerSpec,
    now,
)
from cardea.service.policy import Policy
from cardea.service.schedule import slot_key
from cardea.service.triggers import MemberBusy, remove, upsert

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

API_VERSION = "cardea/v1alpha1"
UNFINISHED = "t.state NOT IN ('succeeded','failed','cancelled')"


def member_id(group: str, pool: str, ordinal: int) -> str:
    return f"{group}.{pool}.{ordinal}"


def trigger_id(member: str, index: int) -> str:
    return f"{member}.t{index}"


def spec_digest(spec: GroupSpec) -> str:
    return sha256(rfc8785.dumps(spec.model_dump(mode="json"))).hexdigest()


def compile_trigger(
    spec: GroupSpec, namespace: str, pool: GroupPool, member: str, index: int, group: str
) -> TriggerSpec:
    """The ADR-029 trigger of one member for one manifest time trigger."""
    trigger = pool.triggers[index]
    assert trigger.schedule is not None, "event triggers are refused before compilation"
    run, bounds = pool.run, pool.run.bounds
    template: dict[str, JsonValue] = {
        "agent_id": member,
        "namespace": namespace,
        "objective": run.objective or f"Agent group {group}, pool {pool.name}",
        "instructions": run.instructions,
        "operations": list(run.operations),
        "grants": list(pool.grants),
        "context": [c.model_dump(mode="json") for c in run.context],
        "expect": [e.model_dump(mode="json") for e in run.expect],
        "harness": run.harness,
        "model": run.model,
        "max_turns": bounds.max_turns,
        "max_tool_calls": bounds.max_tool_calls,
        "max_cost_usd": bounds.max_cost_usd,
        "max_attempts": bounds.attempts,
        "attempt_timeout_seconds": bounds.timeout_seconds,
        "timeout_seconds": bounds.run_timeout_seconds,
    }
    try:
        return TriggerSpec.model_validate(
            {
                "trigger_id": trigger_id(member, index),
                "supervisor_id": spec.supervisor,
                "schedule": trigger.schedule.cron,
                "timezone": trigger.schedule.timezone,
                "misfire": trigger.schedule.misfire,
                "grace_seconds": trigger.schedule.grace_seconds,
                "template_kind": "agent-run",
                "template": template,
            }
        )
    except ValidationError as error:
        reason = error.errors()[0]["msg"] if error.errors() else "invalid"
        raise Fault(
            "invalid_template",
            f"Pool {pool.name}: the run template is not a valid agent run ({reason})",
            422,
        ) from None


def _unsupported(spec: GroupSpec) -> None:
    """Refuse manifest features whose ADR-034 slice has not landed, rather than accept a
    bound the kernel would not enforce."""
    if spec.spend is not None or any(p.spend is not None for p in spec.pools):
        raise Fault(
            "group_spend_unsupported",
            "Group and pool spend accounts arrive with ADR-034 slice 9; remove `spend`",
            422,
        )
    for pool in spec.pools:
        if pool.expect_window:
            raise Fault(
                "group_expect_window_unsupported",
                f"Pool {pool.name}: windowed expectations arrive with ADR-034 slice 9",
                422,
            )
        if any(t.event is not None for t in pool.triggers):
            raise Fault(
                "group_event_trigger_unsupported",
                f"Pool {pool.name}: kernel-journal event triggers arrive with ADR-034 slice 7",
                422,
            )


def _check_grants(policy: Policy, supervisor: str, namespace: str, pool: GroupPool) -> None:
    for grant in pool.grants:
        for subject, mode in (
            ("platform", "allow"),
            (supervisor, "allow"),
            (supervisor, "delegate"),
        ):
            try:
                policy.require(subject, namespace, grant, mode)
            except Fault as error:
                if error.code != "authority_denied":
                    raise
                raise Fault(
                    "group_grant_denied",
                    f"Pool {pool.name} grant {grant} is outside the supervisor's delegable "
                    "authority",
                ) from None


def _members(conn: sa.Connection, namespace: str, name: str) -> list[sa.RowMapping]:
    return list(
        conn.exec_driver_sql(
            "SELECT * FROM group_members WHERE namespace=? AND group_name=? ORDER BY pool, ordinal",
            (namespace, name),
        ).mappings()
    )


def _ensure_member(
    conn: sa.Connection,
    runtime: str,
    namespace: str,
    group: str,
    pool: str,
    ordinal: int,
    at: datetime,
) -> bool:
    """Create a member principal of exactly the form `<group>.<pool>.<n>`; True if new."""
    principal = member_id(group, pool, ordinal)
    row = (
        conn.exec_driver_sql("SELECT * FROM group_members WHERE principal_id=?", (principal,))
        .mappings()
        .first()
    )
    if row is not None:
        if (row["namespace"], row["group_name"], row["pool"], row["ordinal"]) != (
            namespace,
            group,
            pool,
            ordinal,
        ):
            raise Fault(
                "principal_conflict", f"Principal {principal} belongs to another group", 409
            )
        return False
    if conn.exec_driver_sql("SELECT 1 FROM principals WHERE principal_id=?", (principal,)).first():
        raise Fault(
            "principal_conflict",
            f"Principal {principal} already exists and is not a member of this group",
            409,
        )
    conn.exec_driver_sql(
        "INSERT INTO principals(principal_id,kind,disabled,auth_kind,issuer,subject,runtime_id) "
        "VALUES (?,'agent',0,NULL,'','',?)",
        (principal, runtime),
    )
    conn.exec_driver_sql(
        "INSERT INTO group_members VALUES (?,?,?,?,?,'active',?)",
        (principal, namespace, group, pool, ordinal, at.isoformat()),
    )
    return True


def _authorize(conn: sa.Connection, principal: str, namespace: str) -> Policy:
    policy = Policy(conn)
    policy.require(principal, namespace, "groups.manage")
    policy.require("platform", namespace, "groups.manage")
    return policy


def _apply(
    kernel: "Kernel", conn: sa.Connection, proof: Proof, group: AgentGroup
) -> dict[str, Any]:
    principal = proof.resolve(conn)
    spec, name, namespace = group.spec, group.metadata.name, group.metadata.namespace
    if spec.supervisor != principal:
        raise Fault("group_supervisor", "Supervisors manage only their own groups", 403)
    policy = _authorize(conn, principal, namespace)
    _unsupported(spec)
    # `mail:<address>` grants must name exactly one thing (ADR-034 slice 5 review).
    mailbox.check_group_addresses(conn, namespace, name, [p.name for p in spec.pools])
    # TODO(ADR-034 slice 1): check each `spec.collections` entry exists in the namespace
    # under the same supervisor once Collection documents are on main; accepted unchecked.
    at = now()
    old = (
        conn.exec_driver_sql(
            "SELECT * FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
        )
        .mappings()
        .first()
    )
    if old is not None:
        if old["supervisor_id"] != principal:
            raise Fault("group_immutable", "spec.supervisor of a group is immutable", 409)
        previous = GroupSpec.model_validate_json(old["spec_json"])
        wanted = {p.name: p.replicas for p in spec.pools}
        for pool in previous.pools:
            if pool.name not in wanted:
                raise Fault(
                    "group_drain_unsupported",
                    f"Removing pool {pool.name} drains its members, which arrives with "
                    "ADR-034 slice 8",
                    409,
                )
            if wanted[pool.name] < pool.replicas:
                raise Fault(
                    "group_drain_unsupported",
                    f"Scaling pool {pool.name} down drains members, which arrives with "
                    "ADR-034 slice 8",
                    409,
                )
    checksum = spec_digest(spec)
    changed = old is None or old["spec_sha256"] != checksum
    generation = (0 if old is None else int(old["generation"])) + int(changed)
    if old is None:
        conn.exec_driver_sql(
            "INSERT INTO agent_groups VALUES (?,?,?,?,?,?,?,?,?)",
            (
                namespace,
                name,
                principal,
                generation,
                0,
                spec.model_dump_json(),
                checksum,
                at.isoformat(),
                at.isoformat(),
            ),
        )
    elif changed:
        conn.exec_driver_sql(
            "UPDATE agent_groups SET generation=?,spec_json=?,spec_sha256=?,updated_at=? "
            "WHERE namespace=? AND name=?",
            (generation, spec.model_dump_json(), checksum, at.isoformat(), namespace, name),
        )
    plan: dict[str, list[str]] = {
        "members_added": [],
        "triggers_installed": [],
        "triggers_updated": [],
        "triggers_removed": [],
    }
    for pool in spec.pools:
        _check_grants(policy, principal, namespace, pool)
        for ordinal in range(pool.replicas):
            member = member_id(name, pool.name, ordinal)
            if _ensure_member(conn, kernel.runtime_id, namespace, name, pool.name, ordinal, at):
                plan["members_added"].append(member)
            for index in range(len(pool.triggers)):
                trigger = compile_trigger(spec, namespace, pool, member, index, name)
                request = trigger.request(f"{trigger.trigger_id}.{slot_key(at)}")
                kernel._precheck(request, None)
                policy.admission(principal, request, kernel.runtime_id)
                kind = upsert(
                    conn,
                    trigger,
                    kernel.operations,
                    at,
                    managed_by="group",
                    max_task_request_bytes=kernel.max_task_request_bytes,
                    harnesses=kernel.harnesses,
                )
                if kind == "installed":
                    plan["triggers_installed"].append(trigger.trigger_id)
                elif kind != "unchanged":
                    plan["triggers_updated"].append(trigger.trigger_id)
            # Manifest triggers dropped from a pool: in-flight runs keep their requests.
            for index in range(len(pool.triggers), MAX_POOL_TRIGGERS):
                stale = trigger_id(member, index)
                if conn.exec_driver_sql(
                    "SELECT 1 FROM triggers WHERE trigger_id=? AND managed_by='group' "
                    "AND removed=0",
                    (stale,),
                ).first():
                    remove(conn, stale, at)
                    plan["triggers_removed"].append(stale)
    # Slice 4 converges inside this transaction, so the generation is observed on commit.
    conn.exec_driver_sql(
        "UPDATE agent_groups SET observed_generation=? WHERE namespace=? AND name=?",
        (generation, namespace, name),
    )
    if changed:
        with borrow(conn) as tx:
            tx.append_event(
                namespace="kernel.authority",
                event_type="group.applied",
                producer_id=principal,
                correlation_id=f"group-{namespace}-{name}-{generation}",
                at=at,
                payload={
                    "namespace": namespace,
                    "group": name,
                    "generation": generation,
                    "spec_sha256": checksum,
                },
            )
    return {
        "change": "created" if old is None else "updated" if changed else "unchanged",
        "generation": generation,
        "plan": plan,
        "group": _view(conn, namespace, name),
    }


def apply(
    kernel: "Kernel", proof: Proof, name: str, group: AgentGroup, dry_run: bool
) -> dict[str, Any]:
    if group.metadata.name != name:
        raise Fault("group_mismatch", "Path and manifest group names differ", 422)
    with kernel.database.engine.connect() as conn:
        transaction = conn.begin()
        try:
            result = _apply(kernel, conn, proof, group)
        except BaseException:
            transaction.rollback()
            raise
        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
    return {"schema_version": "0.2", "dry_run": dry_run, **result}


def _view(conn: sa.Connection, namespace: str, name: str) -> dict[str, Any]:
    row = (
        conn.exec_driver_sql(
            "SELECT * FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
        )
        .mappings()
        .one()
    )
    members = []
    for member in _members(conn, namespace, name):
        principal = conn.exec_driver_sql(
            "SELECT disabled FROM principals WHERE principal_id=?", (member["principal_id"],)
        ).scalar_one()
        active = conn.exec_driver_sql(
            "SELECT b.task_id FROM task_bindings b JOIN tasks t USING(task_id) "
            f"WHERE b.agent_id=? AND {UNFINISHED} LIMIT 1",
            (member["principal_id"],),
        ).first()
        triggers = [
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT trigger_id FROM triggers WHERE managed_by='group' AND removed=0 "
                "AND json_extract(definition_json,'$.template.agent_id')=? ORDER BY trigger_id",
                (member["principal_id"],),
            )
        ]
        members.append(
            {
                "principal_id": member["principal_id"],
                "pool": member["pool"],
                "ordinal": member["ordinal"],
                "state": member["state"],
                "disabled": bool(principal),
                "active_task_id": active[0] if active else None,
                "triggers": triggers,
            }
        )
    return {
        "schema_version": "0.2",
        "apiVersion": API_VERSION,
        "kind": "AgentGroup",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "generation": row["generation"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        "spec": json.loads(row["spec_json"]),
        "status": {"observedGeneration": row["observed_generation"], "members": members},
    }


def _owned(conn: sa.Connection, principal: str, namespace: str, name: str) -> None:
    row = conn.exec_driver_sql(
        "SELECT supervisor_id FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
    ).first()
    if row is None or row[0] != principal:
        raise Fault("group_not_found", "No accessible group", 404)


def read(kernel: "Kernel", proof: Proof, namespace: str, name: str) -> dict[str, Any]:
    with kernel.database.read() as conn:
        principal = proof.resolve(conn)
        _owned(conn, principal, namespace, name)
        Policy(conn).principal(principal)
        return _view(conn, namespace, name)


def delete(kernel: "Kernel", proof: Proof, namespace: str, name: str, dry_run: bool) -> NoReturn:
    """Deleting a group drains every member (ADR-034 decision 1.5), which is slice 8."""
    with kernel.database.read() as conn:
        principal = proof.resolve(conn)
        _owned(conn, principal, namespace, name)
        _authorize(conn, principal, namespace)
    raise Fault(
        "group_drain_unsupported",
        "Deleting a group drains its members, which arrives with ADR-034 slice 8",
        409,
    )


def check_member(conn: sa.Connection, supervisor: str, request: TaskRequest) -> None:
    """Admission rules for a group member: only its group's supervisor, in the group's
    namespace, admits its runs, and one run at a time (ADR-034 decisions 1.2 and 1.4)."""
    member = (
        conn.exec_driver_sql(
            "SELECT m.namespace,m.state,g.supervisor_id FROM group_members m "
            "JOIN agent_groups g ON g.namespace=m.namespace AND g.name=m.group_name "
            "WHERE m.principal_id=?",
            (request.agent_id,),
        )
        .mappings()
        .first()
    )
    if member is None:
        return
    if member["supervisor_id"] != supervisor or member["namespace"] != request.namespace:
        raise Fault("member_binding", "A group member runs only for its group's supervisor")
    if member["state"] != "active" or not isinstance(request, AgentRunRequest):
        raise Fault("member_binding", "A group member runs only active agent runs")
    # Revoking the supervisor's `groups.manage` stops its members at their next admission.
    policy = Policy(conn)
    policy.require(supervisor, request.namespace, "groups.manage")
    policy.require("platform", request.namespace, "groups.manage")
    busy = conn.exec_driver_sql(
        "SELECT b.task_id FROM task_bindings b JOIN tasks t USING(task_id) "
        f"WHERE b.agent_id=? AND {UNFINISHED} LIMIT 1",
        (request.agent_id,),
    ).first()
    if busy is not None:
        raise MemberBusy(str(busy[0]))
