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

Create, update and scale-up converge inside the apply transaction. Scale-down, pool
removal (a rename is a removal plus an addition) and deletion drain members, highest
ordinal first (slice 8): a draining member gets no new slots, claims or pool mail; its
current run may finish until the pool's `drain_timeout_seconds`, after which it is
cancelled through the normal path. Then its claims are released, its undelivered mail is
re-addressed to its pool (or dead-lettered), its triggers are removed and its principal is
disabled. The reconciler (`converge`) runs in the apply transaction and again on every
minute tick until nothing drains; then `observedGeneration` equals `generation`.
"""

import json
import logging
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import rfc8785
import sqlalchemy as sa
from pydantic import JsonValue, ValidationError

from cardea.kernel.store import KernelTransaction
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
from cardea.service.records import CollectionSpec, check_lease, expire
from cardea.service.schedule import slot_key
from cardea.service.triggers import MemberBusy, remove, upsert

if TYPE_CHECKING:
    from cardea.service.kernel import Kernel

log = logging.getLogger(__name__)
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
    """The ADR-029 trigger of one member for one manifest trigger: a time trigger, or a
    journal trigger on the group's own namespace (ADR-034 decision 6)."""
    trigger = pool.triggers[index]
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
    source: dict[str, JsonValue]
    if trigger.schedule is not None:
        source = {
            "schedule": trigger.schedule.cron,
            "timezone": trigger.schedule.timezone,
            "misfire": trigger.schedule.misfire,
            "grace_seconds": trigger.schedule.grace_seconds,
        }
    else:
        assert trigger.event is not None, "a group trigger has a schedule or an event"
        names = {"$member": member, "$pool": f"{group}.{pool.name}", "$group": group}
        source = {
            "journal": {
                "types": list(trigger.event.types),
                "filters": [
                    f.model_dump(mode="json", exclude_none=True) for f in trigger.event.filters
                ],
                "subjects": [names.get(s, s) for s in trigger.event.subjects],
                "include_own": trigger.event.include_own,
            }
        }
    try:
        return TriggerSpec.model_validate(
            {
                "trigger_id": trigger_id(member, index),
                "supervisor_id": spec.supervisor,
                **source,
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


def _unsupported(spec: GroupSpec, namespace: str) -> None:
    """Refuse manifest features whose ADR-034 slice has not landed, rather than accept a
    bound the kernel would not enforce."""
    if spec.spend is not None or any(p.spend is not None for p in spec.pools):
        raise Fault(
            "group_spend_unsupported",
            "Group and pool spend accounts arrive with ADR-034 slice 9; remove `spend`",
            422,
        )
    journal_source = f"cardea://{namespace}"
    for pool in spec.pools:
        if pool.expect_window:
            raise Fault(
                "group_expect_window_unsupported",
                f"Pool {pool.name}: windowed expectations arrive with ADR-034 slice 9",
                422,
            )
        for trigger in pool.triggers:
            # Group event triggers read the group's own journal; inbound HTTP sources need
            # a host-owner secret, so they stay host-owned (ADR-035).
            if trigger.event is not None and trigger.event.source not in (None, journal_source):
                raise Fault(
                    "group_event_source_unsupported",
                    f"Pool {pool.name}: event triggers read {journal_source}; inbound "
                    "sources are host-owned event triggers",
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
        if row["state"] == "active":
            return False
        # Scaling back up (or re-creating a deleted group) reuses the member principal.
        conn.exec_driver_sql(
            "UPDATE group_members SET state='active',drain_deadline=NULL,drain_generation=NULL "
            "WHERE principal_id=?",
            (principal,),
        )
        conn.exec_driver_sql("UPDATE principals SET disabled=0 WHERE principal_id=?", (principal,))
        return True
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
        "INSERT INTO group_members(principal_id,namespace,group_name,pool,ordinal,state,"
        "created_at) VALUES (?,?,?,?,?,'active',?)",
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
    _unsupported(spec, namespace)
    # `mail:<address>` grants must name exactly one thing (ADR-034 slice 5 review).
    mailbox.check_group_addresses(conn, namespace, name, [p.name for p in spec.pools])
    # Each collection the group uses must exist here under the same supervisor (2.1), with
    # a claim lease long enough for every pool that may claim its records (2.4).
    for collection in spec.collections:
        row = conn.exec_driver_sql(
            "SELECT supervisor_id,spec_json FROM collections WHERE namespace=? AND name=? "
            "AND deleted_at IS NULL",
            (namespace, collection),
        ).first()
        if row is None or row[0] != principal:
            raise Fault(
                "collection_not_found",
                f"Collection {collection} does not exist in this namespace under this supervisor",
                422,
            )
        check_lease(collection, CollectionSpec.model_validate_json(row[1]), spec.pools)
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
        if old["state"] == "deleting":
            raise Fault("group_deleting", "The group is still draining after its deletion", 409)
    checksum = spec_digest(spec)
    changed = old is None or old["spec_sha256"] != checksum or old["state"] == "deleted"
    generation = (0 if old is None else int(old["generation"])) + int(changed)
    if old is None:
        conn.exec_driver_sql(
            "INSERT INTO agent_groups(namespace,name,supervisor_id,generation,"
            "observed_generation,spec_json,spec_sha256,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
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
            "UPDATE agent_groups SET generation=?,spec_json=?,spec_sha256=?,updated_at=?,"
            "state='active' WHERE namespace=? AND name=?",
            (generation, spec.model_dump_json(), checksum, at.isoformat(), namespace, name),
        )
    plan: dict[str, list[str]] = {
        "members_added": [],
        "members_draining": [],
        "triggers_installed": [],
        "triggers_updated": [],
        "triggers_removed": [],
    }
    # Scale-down and pool removal drain the members the spec no longer has, highest
    # ordinal first; their current runs keep their bound requests (decision 1.5).
    if old is not None and changed:
        previous = {p.name: p for p in GroupSpec.model_validate_json(old["spec_json"]).pools}
        wanted = {p.name: p.replicas for p in spec.pools}
        for drained in sorted(
            _members(conn, namespace, name), key=lambda m: (m["pool"], -int(m["ordinal"]))
        ):
            if drained["state"] != "active":
                continue
            if int(drained["ordinal"]) < wanted.get(str(drained["pool"]), 0):
                continue
            pool_spec = previous.get(str(drained["pool"]))
            timeout = pool_spec.drain_timeout_seconds if pool_spec else 0
            plan["triggers_removed"] += _remove_triggers(conn, str(drained["principal_id"]), at)
            _start_drain(conn, drained, timeout, generation, at)
            plan["members_draining"].append(str(drained["principal_id"]))
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
    # Converge what can be converged now; draining members finish on later ticks.
    outcome = converge(kernel, conn, namespace, name, at)
    plan["members_removed"] = outcome["members_removed"]
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
        "status": {
            "observedGeneration": row["observed_generation"],
            "state": row["state"],
            "members": members,
        },
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


def delete(
    kernel: "Kernel", proof: Proof, namespace: str, name: str, dry_run: bool
) -> dict[str, Any]:
    """Deleting a group drains every member (decision 1.5). The group is `deleting` until
    the last member is removed, then `deleted`; `dry_run` returns the plan only."""
    with kernel.database.engine.connect() as conn:
        transaction = conn.begin()
        try:
            principal = proof.resolve(conn)
            _owned(conn, principal, namespace, name)
            _authorize(conn, principal, namespace)
            at = now()
            row = (
                conn.exec_driver_sql(
                    "SELECT * FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
                )
                .mappings()
                .one()
            )
            spec = GroupSpec.model_validate_json(row["spec_json"])
            timeouts = {p.name: p.drain_timeout_seconds for p in spec.pools}
            draining: list[str] = []
            if row["state"] == "active":
                conn.exec_driver_sql(
                    "UPDATE agent_groups SET state='deleting',updated_at=? "
                    "WHERE namespace=? AND name=?",
                    (at.isoformat(), namespace, name),
                )
                for member in sorted(
                    _members(conn, namespace, name),
                    key=lambda m: (m["pool"], -int(m["ordinal"])),
                ):
                    if member["state"] == "active":
                        timeout = timeouts.get(str(member["pool"]), 0)
                        _start_drain(conn, member, timeout, int(row["generation"]), at)
                        draining.append(str(member["principal_id"]))
            outcome = converge(kernel, conn, namespace, name, at)
            state = conn.exec_driver_sql(
                "SELECT state FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
            ).scalar_one()
        except BaseException:
            transaction.rollback()
            raise
        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
    return {
        "schema_version": "0.2",
        "name": name,
        "namespace": namespace,
        "dry_run": dry_run,
        "change": state,
        "members_draining": draining,
        "members_removed": outcome["members_removed"],
        "waiting": outcome["waiting"],
    }


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


# Draining (ADR-034 decision 1.5, slice 8)


def _start_drain(
    conn: sa.Connection,
    member: sa.RowMapping,
    timeout_seconds: int,
    generation: int,
    at: datetime,
) -> None:
    """A draining member gets no new slots, claims or pool mail; its run may finish."""
    deadline = at + timedelta(seconds=timeout_seconds)
    conn.exec_driver_sql(
        "UPDATE group_members SET state='draining',drain_deadline=?,drain_generation=? "
        "WHERE principal_id=? AND state='active'",
        (deadline.isoformat(), generation, member["principal_id"]),
    )
    _remove_triggers(conn, str(member["principal_id"]), at)


def _remove_triggers(conn: sa.Connection, principal: str, at: datetime) -> list[str]:
    removed = [
        str(r[0])
        for r in conn.exec_driver_sql(
            "SELECT trigger_id FROM triggers WHERE managed_by='group' AND removed=0 "
            "AND json_extract(definition_json,'$.template.agent_id')=? ORDER BY trigger_id",
            (principal,),
        )
    ]
    for trigger in removed:
        remove(conn, trigger, at)
    return removed


def _unfinished(conn: sa.Connection, principal: str) -> str | None:
    row = conn.exec_driver_sql(
        "SELECT b.task_id FROM task_bindings b JOIN tasks t USING(task_id) "
        f"WHERE b.agent_id=? AND {UNFINISHED} LIMIT 1",
        (principal,),
    ).first()
    return str(row[0]) if row else None


def _finish(
    kernel: "Kernel",
    conn: sa.Connection,
    tx: KernelTransaction,
    group: sa.RowMapping,
    member: sa.RowMapping,
    at: datetime,
) -> None:
    """A drained member with no unfinished run: release its claims, re-address its mail,
    remove its triggers and disable its principal (decision 1.5)."""
    principal = str(member["principal_id"])
    namespace = str(member["namespace"])
    runtime = kernel.runtime_id
    for row in (
        conn.exec_driver_sql(
            "SELECT r.*, c.spec_json FROM records r JOIN collections c "
            "ON c.namespace=r.namespace AND c.name=r.collection WHERE r.holder=?",
            (principal,),
        )
        .mappings()
        .all()
    ):
        spec = CollectionSpec.model_validate_json(row["spec_json"])
        expire(conn, row, at, actor=runtime, reason="member_removed", spec=spec)
    mailbox.sweep(conn, tx, runtime, at)
    pool = f"{member['group_name']}.{member['pool']}"
    open_pool = group["state"] == "active" and mailbox.member_pool_open(conn, namespace, pool)
    mailbox.readdress(conn, tx, namespace, principal, pool if open_pool else None, runtime, at)
    _remove_triggers(conn, principal, at)
    conn.exec_driver_sql("UPDATE principals SET disabled=1 WHERE principal_id=?", (principal,))
    conn.exec_driver_sql(
        "UPDATE group_members SET state='removed',drain_deadline=NULL WHERE principal_id=?",
        (principal,),
    )
    tx.append_event(
        namespace="kernel.authority",
        event_type="group.member_removed",
        producer_id=runtime,
        correlation_id=principal,
        at=at,
        payload={"namespace": namespace, "group": member["group_name"], "member": principal},
    )


def converge(
    kernel: "Kernel", conn: sa.Connection, namespace: str, name: str, at: datetime
) -> dict[str, list[str]]:
    """One reconciler pass over a group, in the caller's transaction; restartable.

    Draining members are taken highest ordinal first. A member whose run is unfinished
    waits until its drain deadline, then the run is cancelled through the normal path
    (uncertain effects stay `outcome_unknown`) and the member finishes on a later pass.
    The generation is observed once nothing drains; a deleting group becomes deleted.
    """
    group = (
        conn.exec_driver_sql(
            "SELECT * FROM agent_groups WHERE namespace=? AND name=?", (namespace, name)
        )
        .mappings()
        .one()
    )
    outcome: dict[str, list[str]] = {"members_removed": [], "runs_cancelled": [], "waiting": []}
    draining = list(
        conn.exec_driver_sql(
            "SELECT * FROM group_members WHERE namespace=? AND group_name=? AND state='draining' "
            "ORDER BY pool, ordinal DESC",
            (namespace, name),
        ).mappings()
    )
    with borrow(conn) as tx:
        for member in draining:
            principal = str(member["principal_id"])
            task_id = _unfinished(conn, principal)
            if task_id is not None:
                if at < datetime.fromisoformat(str(member["drain_deadline"])):
                    outcome["waiting"].append(principal)
                    continue
                kernel.signal(
                    conn,
                    tx,
                    task_id,
                    namespace,
                    str(group["supervisor_id"]),
                    f"drain.{principal}.{member['drain_generation']}",
                    "cancel",
                )
                outcome["runs_cancelled"].append(task_id)
                if _unfinished(conn, principal) is not None:
                    outcome["waiting"].append(principal)
                    continue
            _finish(kernel, conn, tx, group, member, at)
            outcome["members_removed"].append(principal)
        if not outcome["waiting"]:
            state = "deleted" if group["state"] in ("deleting", "deleted") else group["state"]
            conn.exec_driver_sql(
                "UPDATE agent_groups SET observed_generation=generation,state=?,updated_at=? "
                "WHERE namespace=? AND name=?",
                (state, at.isoformat(), namespace, name),
            )
    return outcome


def reconcile_all(kernel: "Kernel", at: datetime | None = None) -> dict[str, dict[str, list[str]]]:
    """The minute tick's pass over every group that has not converged (ADR-024's clock)."""
    at = at or now()
    with kernel.database.read() as conn:
        pending = [
            (str(r[0]), str(r[1]))
            for r in conn.exec_driver_sql(
                "SELECT namespace,name FROM agent_groups "
                "WHERE observed_generation<generation OR state='deleting' ORDER BY namespace,name"
            )
        ]
    results: dict[str, dict[str, list[str]]] = {}
    for namespace, name in pending:
        try:
            with kernel.database.engine.begin() as conn:
                results[f"{namespace}/{name}"] = converge(kernel, conn, namespace, name, at)
        except Exception:
            # A broken group must not stop the others; the next tick retries it.
            log.exception("group reconciliation failed", extra={"code": f"{namespace}/{name}"})
    return results
