"""Collections and records (ADR-034 slice 1).

A collection is a supervisor-managed document in one namespace: a JSON Schema 2020-12 for
its records' data, a size bound, and optional CEL write guards. Records are versioned rows
with labels and links; every write is compare-and-set on ``version`` and appends one row to
the append-only ``record_events`` history in the same transaction.

The four built-in operations ``records.create``, ``records.get``, ``records.query`` and
``records.update`` are in-process installed operations. They are served on the per-attempt
tool endpoint and as operation tasks, and receipted by the shared invocation core like any
installed operation (ADR-023, ADR-026). Each call needs the resource-qualified grant
``record:<collection>/<action>``, checked under the live ceiling inside the transaction
that reads or writes the record. Guards reuse ADR-036's CEL compilation and limits.

Claims, state machines, groups and mailboxes are later slices; the table columns they need
already exist (migration ``m10_records``).
"""

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from cardea.kernel.models import Identifier, OperationReceipt, OperationState
from cardea.service.approvals import execute_condition
from cardea.service.database import Database, Fault, borrow
from cardea.service.models import (
    COLLECTION_NAME,
    RECORD_ACTION,
    CollectionName,
    GroupPool,
    GroupSpec,
    Record,
    now,
    record_grant,
)
from cardea.service.operations import (
    Handler,
    OperationContext,
    OperationDescriptor,
    OperationRejected,
    Reconciler,
    Reconciliation,
)
from cardea.service.policy import Policy
from cardea.service.record_states import (
    GUARD_VARIABLES,
    MAX_STATES,
    MAX_TRANSITIONS,
    GuardSource,
    ReceiptRequirement,
    StateMigration,
    StateName,
    Transition,
    approval_rule,
    check_table,
    claiming_grants,
)

API_VERSION = "cardea/v1alpha1"
MAX_LABELS = 16
MAX_LINKS = 32
DEFAULT_RECORD_BYTES = 16384
MAX_RECORD_BYTES = 49152
MAX_QUERY_LIMIT = 100
# Room left in a 64 KiB receipt for one query page and its wrapper.
MAX_PAGE_BYTES = 57344
FIELD = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"

Label = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,63}$")]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class Guards(Record):
    """Optional CEL write guards over ``record``, ``args`` and ``caller``; they only narrow."""

    create: GuardSource | None = None
    update: GuardSource | None = None
    # Standalone claims (ADR-034 slice 2); state-machine transitions guard themselves.
    claim: GuardSource | None = None
    release: GuardSource | None = None


DEFAULT_LEASE_SECONDS = 14400  # carried decision 5: four hours
MIN_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 30 * 24 * 3600
# A holder whose runs show it is alive keeps its claim at least this long past an attempt.
ATTEMPT_RENEWAL_MARGIN_SECONDS = 120
# The apply-time bound adds this to 2 x attempts x attempt timeout (decision 2.4).
LEASE_BOUND_MARGIN_SECONDS = 240


class ClaimsConfig(Record):
    """Claims on a collection's records (ADR-034 decision 2.4).

    A claim belongs to a principal, not a run. Without a state machine (slice 3) claims are
    taken and dropped with `records.claim` and `records.release`, which need `standalone`.
    """

    lease_seconds: Annotated[
        int, Field(strict=True, ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)
    ] = DEFAULT_LEASE_SECONDS
    max_per_holder: Annotated[int, Field(strict=True, ge=1, le=64)] = 1
    max_followups: Annotated[int, Field(strict=True, ge=0, le=100)] = 6
    standalone: Annotated[bool, Field(strict=True)] = False
    # While a record is held, writes need the current holder and `claim_epoch` (lead,
    # 2026-11-09). `false` leaves held records writable by anyone the grant and guards allow.
    fence_writes: Annotated[bool, Field(strict=True)] = True


def lease_bound(attempts: int, timeout_seconds: int) -> int:
    """The shortest lease a pool that may claim needs: two full runs plus margin."""
    return 2 * attempts * timeout_seconds + LEASE_BOUND_MARGIN_SECONDS


def check_lease(collection: str, spec: "CollectionSpec", pools: Iterable[GroupPool]) -> None:
    """Refuse a lease shorter than `lease_bound` for any pool that may hold a record: one
    granted `record:<C>/claim` or an acquiring transition."""
    claims = spec.claims
    if claims is None:
        return
    claiming = claiming_grants(collection, spec.transitions)
    for pool in pools:
        if not claiming & set(pool.grants):
            continue
        bound = lease_bound(pool.run.bounds.attempts, pool.run.bounds.timeout_seconds)
        if claims.lease_seconds < bound:
            raise Fault(
                "claim_lease_too_short",
                f"Pool {pool.name}: collection {collection} has a {claims.lease_seconds} s "
                f"claim lease, below {bound} s (2 x attempts x attempt timeout + 240)",
                422,
            )


def referencing_groups(conn: sa.Connection, namespace: str, name: str) -> list[GroupSpec]:
    """The specs of every group in the namespace that lists this collection."""
    specs = [
        GroupSpec.model_validate_json(row[0])
        for row in conn.exec_driver_sql(
            "SELECT spec_json FROM agent_groups WHERE namespace=?", (namespace,)
        )
    ]
    return [spec for spec in specs if name in spec.collections]


class CollectionSpec(Record):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, populate_by_name=True
    )

    supervisor: Identifier
    json_schema: dict[str, JsonValue] = Field(alias="schema")
    max_record_bytes: Annotated[int, Field(strict=True, ge=64, le=MAX_RECORD_BYTES)] = (
        DEFAULT_RECORD_BYTES
    )
    guards: Guards = Guards()
    claims: ClaimsConfig | None = None
    # A flat state machine (decision 3); absent means plain records.
    states: Annotated[tuple[StateName, ...], Field(min_length=1, max_length=MAX_STATES)] | None = (
        None
    )
    initial: StateName | None = None
    terminal: Annotated[tuple[StateName, ...], Field(max_length=MAX_STATES)] = ()
    transitions: Annotated[tuple[Transition, ...], Field(max_length=MAX_TRANSITIONS)] = ()
    # Where records in states this generation removes go (decision 3.3).
    migrate: Annotated[tuple[StateMigration, ...], Field(max_length=MAX_STATES)] = ()

    def transition(self, name: str) -> Transition | None:
        return next((t for t in self.transitions if t.name == name), None)

    @model_validator(mode="after")
    def valid_table(self) -> Self:
        check_table(
            self.states,
            self.initial,
            self.terminal,
            self.transitions,
            self.migrate,
            self.claims is not None,
        )
        for transition in self.transitions:
            # Compiles the gate's rule now, so its CEL limits are checked at apply.
            approval_rule("c", transition)
        return self

    @model_validator(mode="after")
    def valid_schema(self) -> Self:
        try:
            Draft202012Validator.check_schema(self.json_schema)
        except SchemaError as error:
            raise ValueError("Collection schema is not valid JSON Schema 2020-12") from error
        if self.json_schema.get("type") != "object":
            raise ValueError("Collection schemas must describe objects")
        return self


class CollectionMetadata(Record):
    name: CollectionName
    namespace: Identifier


class CollectionDocument(Record):
    """`kind: Collection` (ADR-034 decisions 2 and 3)."""

    apiVersion: Literal["cardea/v1alpha1"] = "cardea/v1alpha1"  # noqa: N815 - Kubernetes shape
    kind: Literal["Collection"] = "Collection"
    metadata: CollectionMetadata
    spec: CollectionSpec

    def spec_json(self) -> dict[str, JsonValue]:
        """The stored spec. Absent later-slice fields stay absent, so a collection applied
        before they existed keeps its digest and generation."""
        spec = self.spec.model_dump(mode="json", by_alias=True)
        claims = spec.get("claims")
        if claims is None:
            spec.pop("claims", None)
        elif isinstance(claims, dict) and claims.get("fence_writes") is True:
            # The default is recorded by its absence, like other later-added fields.
            claims.pop("fence_writes")
        guards = spec["guards"]
        assert isinstance(guards, dict)
        for action in ("claim", "release"):
            if guards.get(action) is None:
                guards.pop(action, None)
        for key in ("states", "initial", "terminal", "transitions", "migrate"):
            if spec.get(key) in (None, []):
                spec.pop(key, None)
        return spec

    @property
    def digest(self) -> str:
        return sha256(rfc8785.dumps(self.spec_json())).hexdigest()


class Link(Record):
    rel: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    url: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] | None = None
    external_id: Text | None = None
    observed_state: Annotated[str, Field(strict=True, max_length=64)] | None = None
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def target(self) -> Self:
        if self.url is None and self.external_id is None:
            raise ValueError("A link needs a url or an external_id")
        return self


# Collection documents


def _row(conn: sa.Connection, namespace: str, name: str) -> sa.RowMapping | None:
    return (
        conn.exec_driver_sql(
            "SELECT * FROM collections WHERE namespace=? AND name=?", (namespace, name)
        )
        .mappings()
        .first()
    )


def _document_view(row: sa.RowMapping, records: int) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": "Collection",
        "metadata": {
            "name": row["name"],
            "namespace": row["namespace"],
            "generation": row["generation"],
        },
        "spec": json.loads(row["spec_json"]),
        # Applied in the PUT transaction, so status always matches the spec.
        "status": {"observedGeneration": row["generation"], "records": records},
    }


def _count(conn: sa.Connection, namespace: str, name: str) -> int:
    return int(
        conn.exec_driver_sql(
            "SELECT COUNT(*) FROM records WHERE namespace=? AND collection=?", (namespace, name)
        ).scalar_one()
    )


def apply_collection(
    conn: sa.Connection, principal: str, document: CollectionDocument, *, dry_run: bool
) -> dict[str, Any]:
    """`PUT /v0.2/collections/{name}`: the caller must be the supervisor and hold
    `groups.manage` in the namespace. An unchanged spec keeps its generation."""
    namespace, name = document.metadata.namespace, document.metadata.name
    if document.spec.supervisor != principal:
        raise Fault("collection_supervisor", "The caller must be the collection's supervisor")
    policy = Policy(conn)
    policy.require(principal, namespace, "groups.manage")
    policy.require("platform", namespace, "groups.manage")
    existing = _row(conn, namespace, name)
    spec = document.spec_json()
    digest = document.digest
    if existing is not None and existing["supervisor_id"] != principal:
        # A collection belongs to one supervisor; this also hides it from other callers.
        raise Fault("collection_supervisor", "The collection belongs to another supervisor")
    # A shorter lease must still fit every group pool that may claim (decision 2.4).
    for group in referencing_groups(conn, namespace, name):
        check_lease(name, document.spec, group.pools)
    if existing is None:
        change, generation = "created", 1
    elif existing["spec_digest"] == digest:
        change, generation = "unchanged", int(existing["generation"])
    else:
        change, generation = "updated", int(existing["generation"]) + 1
    moves = plan_moves(conn, namespace, name, document.spec)
    plan: dict[str, Any] = {
        "change": change,
        "generation": generation,
        "dry_run": dry_run,
        "migrated": [{"from": old, "to": new, "records": n} for old, new, n in moves],
    }
    if dry_run or change == "unchanged":
        view = (
            _document_view(existing, _count(conn, namespace, name))
            if existing is not None and change == "unchanged"
            else {
                "apiVersion": API_VERSION,
                "kind": "Collection",
                "metadata": {"name": name, "namespace": namespace, "generation": generation},
                "spec": spec,
                "status": {
                    "observedGeneration": existing["generation"] if existing else None,
                    "records": _count(conn, namespace, name) if existing else 0,
                },
            }
        )
        return {**view, "plan": plan}
    at = now().isoformat()
    if existing is None:
        conn.exec_driver_sql(
            "INSERT INTO collections VALUES (?,?,?,?,?,?,?,?)",
            (namespace, name, principal, generation, json.dumps(spec), digest, at, at),
        )
    else:
        conn.exec_driver_sql(
            "UPDATE collections SET generation=?,spec_json=?,spec_digest=?,updated_at=? "
            "WHERE namespace=? AND name=?",
            (generation, json.dumps(spec), digest, at, namespace, name),
        )
    apply_moves(conn, namespace, name, moves, principal, generation)
    row = _row(conn, namespace, name)
    assert row is not None
    return {**_document_view(row, _count(conn, namespace, name)), "plan": plan}


Move = tuple[str | None, str, int]


def plan_moves(
    conn: sa.Connection, namespace: str, name: str, spec: "CollectionSpec"
) -> list[Move]:
    """Records this generation moves to another state (decision 3.3).

    A state that records still occupy may be removed only with a `migrate` entry. Records
    without a state join the `initial` state when a collection gains a state machine.
    """
    states = set(spec.states or ())
    migrate = {m.from_: m.to for m in spec.migrate}
    moves: list[Move] = []
    for state, count in conn.exec_driver_sql(
        "SELECT state, COUNT(*) FROM records WHERE namespace=? AND collection=? "
        "GROUP BY state ORDER BY state",
        (namespace, name),
    ):
        if state is None:
            if spec.initial is not None:
                moves.append((None, spec.initial, int(count)))
            continue
        if state in states:
            continue
        if state not in migrate:
            raise Fault(
                "state_occupied",
                f"{count} record(s) are in state {state}; remove it with migrate "
                f"{{from: {state}, to: <state>}}",
                409,
            )
        moves.append((str(state), migrate[state], int(count)))
    return moves


def apply_moves(
    conn: sa.Connection,
    namespace: str,
    name: str,
    moves: list[Move],
    actor: str,
    generation: int,
) -> None:
    """Move records as recorded `migrated` changes; their history is kept."""
    at = now()
    for source, target, _ in moves:
        rows = (
            conn.exec_driver_sql(
                "SELECT * FROM records WHERE namespace=? AND collection=? AND state IS ?",
                (namespace, name, source),
            )
            .mappings()
            .all()
        )
        for row in rows:
            old = record_view(row)
            new = {
                **old,
                "state": target,
                "version": int(row["version"]) + 1,
                "updated_at": at.isoformat(),
            }
            conn.exec_driver_sql(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE seq=?",
                (target, new["version"], new["updated_at"], row["seq"]),
            )
            detail: dict[str, JsonValue] = {"from": source, "to": target, "generation": generation}
            append_history(conn, namespace, "migrated", actor, None, None, old, new, detail)
            journal(conn, namespace, "migrated", new, actor, None, at, detail)


def append_history(
    conn: sa.Connection,
    namespace: str,
    kind: str,
    actor: str,
    task_id: str | None,
    receipt_id: str | None,
    old: Mapping[str, JsonValue] | None,
    new: Mapping[str, JsonValue],
    detail: Mapping[str, JsonValue] | None = None,
) -> None:
    """One append-only history row for a change of one record."""
    conn.exec_driver_sql(
        "INSERT INTO record_events(namespace,collection,record_id,version,kind,actor,"
        "task_id,receipt_id,old_json,new_json,at,detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            namespace,
            new["collection"],
            new["record_id"],
            new["version"],
            kind,
            actor,
            task_id,
            receipt_id,
            json.dumps(old) if old is not None else None,
            json.dumps(new),
            new["updated_at"],
            json.dumps(detail) if detail is not None else None,
        ),
    )


def journal(
    conn: sa.Connection,
    namespace: str,
    kind: str,
    record: Mapping[str, JsonValue],
    producer: str,
    task_id: str | None,
    at: datetime,
    data: Mapping[str, JsonValue],
) -> None:
    """A kernel journal event in CloudEvents 1.0 shape, in the change's transaction.

    `type` is `cardea.record.<kind>` and `subject` `<collection>/<record_id>` (decision 6), so
    slice 7 can match transitions, claims and expiry like any ADR-035 event.
    """
    with borrow(conn) as tx:
        tx.append_event(
            namespace=namespace,
            event_type=f"cardea.record.{kind}",
            producer_id=producer,
            correlation_id=f"{record['record_id']}.{record['version']}",
            task_id=task_id,
            at=at,
            payload={
                "specversion": "1.0",
                "id": f"{record['record_id']}.{record['version']}.{kind}",
                "source": f"cardea://{namespace}",
                "type": f"cardea.record.{kind}",
                "subject": f"{record['collection']}/{record['record_id']}",
                "time": at.isoformat(),
                "data": {
                    "collection": record["collection"],
                    "record_id": record["record_id"],
                    "version": record["version"],
                    "state": record["state"],
                    "holder": record["holder"],
                    "claim_epoch": record["claim_epoch"],
                    **data,
                },
            },
        )


def read_access(conn: sa.Connection, principal: str, namespace: str, name: str) -> sa.RowMapping:
    """The collection's supervisor (with `groups.manage`) or a holder of `record:<C>/read`."""
    policy = Policy(conn)
    row = _row(conn, namespace, name)
    try:
        right = (
            "groups.manage"
            if row is not None and row["supervisor_id"] == principal
            else record_grant(name, "read")
        )
        policy.require(principal, namespace, right)
        policy.require("platform", namespace, right)
    except Fault:
        raise Fault("collection_not_found", "No accessible collection", 404) from None
    if row is None:
        raise Fault("collection_not_found", "No accessible collection", 404)
    return row


def get_collection(
    conn: sa.Connection, principal: str, namespace: str, name: str
) -> dict[str, Any]:
    row = read_access(conn, principal, namespace, name)
    return _document_view(row, _count(conn, namespace, name))


def history(
    conn: sa.Connection,
    principal: str,
    namespace: str,
    name: str,
    record_id: str,
    after: int,
    limit: int,
) -> dict[str, Any]:
    """A record's append-only history, oldest first, paged by `event_id`."""
    read_access(conn, principal, namespace, name)
    rows = (
        conn.exec_driver_sql(
            "SELECT * FROM record_events WHERE namespace=? AND collection=? AND record_id=? "
            "AND event_id>? ORDER BY event_id LIMIT ?",
            (namespace, name, record_id, after, limit + 1),
        )
        .mappings()
        .all()
    )
    if not rows and not _record_row(conn, namespace, name, record_id):
        raise Fault("record_not_found", "No such record", 404)
    page = rows[:limit]
    return {
        "events": [
            {
                "event_id": r["event_id"],
                "version": r["version"],
                "kind": r["kind"],
                "actor": r["actor"],
                "task_id": r["task_id"],
                "receipt_id": r["receipt_id"],
                "old": json.loads(r["old_json"]) if r["old_json"] else None,
                "new": json.loads(r["new_json"]),
                "at": r["at"],
            }
            for r in page
        ],
        "next_after": page[-1]["event_id"] if len(rows) > limit else None,
    }


# Records


def _record_row(
    conn: sa.Connection, namespace: str, collection: str, record_id: str
) -> sa.RowMapping | None:
    return (
        conn.exec_driver_sql(
            "SELECT * FROM records WHERE namespace=? AND collection=? AND record_id=?",
            (namespace, collection, record_id),
        )
        .mappings()
        .first()
    )


def record_view(row: Any) -> dict[str, JsonValue]:
    return {
        "collection": row["collection"],
        "record_id": row["record_id"],
        "data": json.loads(row["data_json"]),
        "labels": json.loads(row["labels_json"]),
        "links": json.loads(row["links_json"]),
        "state": row["state"],
        "holder": row["holder"],
        "claim_epoch": row["claim_epoch"],
        "lease_expires_at": row["lease_expires_at"],
        "version": row["version"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_LABELS: TypeAdapter[list[str]] = TypeAdapter(Annotated[list[Label], Field(max_length=MAX_LABELS)])
_LINKS: TypeAdapter[list[Link]] = TypeAdapter(Annotated[list[Link], Field(max_length=MAX_LINKS)])


def _labels(value: object) -> list[JsonValue]:
    try:
        labels = _LABELS.validate_python(value)
    except ValidationError:
        raise OperationRejected(
            f"labels must be at most {MAX_LABELS} label strings", "record_invalid"
        ) from None
    if len(set(labels)) != len(labels):
        raise OperationRejected("labels must be distinct", "record_invalid")
    return [*labels]


def _links(value: object) -> list[JsonValue]:
    try:
        links = _LINKS.validate_python(value)
    except ValidationError:
        raise OperationRejected(
            f"links must be at most {MAX_LINKS} links, each with rel and a url or external_id",
            "record_invalid",
        ) from None
    return [link.model_dump(mode="json") for link in links]


def _data(spec: CollectionSpec, value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise OperationRejected("data must be an object", "record_invalid")
    size = len(rfc8785.dumps(value))
    if size > spec.max_record_bytes:
        raise OperationRejected(
            f"data is {size} bytes; the collection allows {spec.max_record_bytes}",
            "record_too_large",
        )
    error = next(iter(Draft202012Validator(spec.json_schema).iter_errors(value)), None)
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        # Name the failing location and validator only; values may be sensitive.
        raise OperationRejected(
            f"data does not match the collection schema at {where} ({error.validator})",
            "record_invalid",
        )
    return value


def check_guard(
    spec: CollectionSpec,
    action: str,
    record: Mapping[str, JsonValue],
    args: Mapping[str, JsonValue],
    caller: Mapping[str, JsonValue],
) -> None:
    """A collection's write guard for one action (decision 2.3); fails closed."""
    check_condition(getattr(spec.guards, action, None), action, record, args, caller)


def check_condition(
    source: str | None,
    label: str,
    record: Mapping[str, JsonValue],
    args: Mapping[str, JsonValue],
    caller: Mapping[str, JsonValue],
) -> None:
    """Fail closed: a false guard, or one that errors, refuses the call (decisions 2.3, 3.1)."""
    if source is None:
        return
    value, error = execute_condition(
        source,
        {"record": dict(record), "args": dict(args), "caller": dict(caller)},
        GUARD_VARIABLES,
    )
    if error is not None:
        raise OperationRejected(f"{label} guard failed to evaluate: {error}", "guard_refused")
    if value is not True:
        raise OperationRejected(f"{label} guard refused this call", "guard_refused")


def _names_record(receipt: OperationReceipt, record_id: str) -> bool:
    """A call was made for a record: its `record_id` argument, or a claim idempotency
    context `<record_id>.<epoch>`."""
    recorded = receipt.request.arguments
    called = recorded.get("arguments")
    provenance = recorded.get("context")
    return (isinstance(called, dict) and called.get("record_id") == record_id) or (
        isinstance(provenance, dict)
        and str(provenance.get("idempotency_context") or "").startswith(f"{record_id}.")
    )


def _latest_receipt(
    conn: sa.Connection, task_id: str, operation: str, record_id: str
) -> str | None:
    rows = conn.exec_driver_sql(
        "SELECT snapshot FROM operations WHERE task_id=? AND state='succeeded' "
        "AND json_extract(snapshot,'$.request.name')=? ORDER BY rowid DESC",
        (task_id, operation),
    ).fetchall()
    for (snapshot,) in rows:
        receipt = OperationReceipt.model_validate_json(snapshot)
        if _names_record(receipt, record_id):
            return receipt.receipt_id
    return None


def check_receipt(
    conn: sa.Connection,
    need: ReceiptRequirement,
    arguments: Mapping[str, JsonValue],
    context: OperationContext,
    caller: str,
    record_id: str,
) -> str:
    """The evidence a transition needs (decision 3.1): the cited receipt is a successful,
    or reconciled-applied, call of the named operation, made by the caller's task (or, for
    `same_caller`, by the caller within the window), for this record.

    An agent sees operation outputs, not receipt IDs, so with `same_task` an omitted
    `receipt_id` cites the task's latest successful call of the operation for this record.
    """
    receipt_id = arguments.get("receipt_id")
    if receipt_id is None and need.within == "same_task":
        receipt_id = _latest_receipt(conn, context.task_id, need.operation, record_id)
    if not isinstance(receipt_id, str):
        raise OperationRejected(
            f"This transition needs receipt_id of a {need.operation} call", "receipt_required"
        )
    row = conn.exec_driver_sql(
        "SELECT o.snapshot, b.agent_id FROM operations o JOIN task_bindings b USING(task_id) "
        "WHERE o.receipt_id=?",
        (receipt_id,),
    ).first()
    receipt = OperationReceipt.model_validate_json(row[0]) if row is not None else None
    if receipt is None or receipt.request.name != need.operation:
        raise OperationRejected(f"Cite a receipt of {need.operation}", "receipt_invalid")
    if receipt.state is not OperationState.SUCCEEDED:
        raise OperationRejected("The cited call did not succeed", "receipt_invalid")
    if need.within == "same_task":
        if receipt.request.task_id != context.task_id:
            raise OperationRejected("The cited call belongs to another task", "receipt_invalid")
    else:
        assert need.window_seconds is not None
        if row is None or row[1] != caller:
            raise OperationRejected("The cited call was made by someone else", "receipt_invalid")
        if receipt.updated_at < now() - timedelta(seconds=need.window_seconds):
            raise OperationRejected("The cited call is outside the window", "receipt_invalid")
    if not _names_record(receipt, record_id):
        raise OperationRejected("The cited call was not made for this record", "receipt_invalid")
    return receipt_id


# Claims (ADR-034 decision 2.4)

# Built-in operations that change a record; their reconciler reads the history.
WRITES = frozenset(
    {"records.create", "records.update", "records.claim", "records.release", "records.transition"}
)


def stamp(at: datetime) -> str:
    """Lease times in one fixed format, so SQL string comparison orders them."""
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def caller_of(conn: sa.Connection, principal: str) -> dict[str, JsonValue]:
    """The guard's `caller`: the principal, and its group and pool when it is a member."""
    member = conn.exec_driver_sql(
        "SELECT group_name,pool FROM group_members WHERE principal_id=?", (principal,)
    ).first()
    return {
        "id": principal,
        "group": str(member[0]) if member else None,
        "pool": f"{member[0]}.{member[1]}" if member else None,
    }


def claim_context(record_id: str, epoch: int) -> str:
    """The ADR-026 idempotency context for effects taken for one claim epoch."""
    return f"{record_id}.{epoch}"


def claim_view(record: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    epoch = record["claim_epoch"]
    assert isinstance(epoch, int)
    return {
        "collection": record["collection"],
        "record_id": record["record_id"],
        "claim_epoch": epoch,
        "lease_expires_at": record["lease_expires_at"],
        "idempotency_context": claim_context(str(record["record_id"]), epoch),
    }


def standalone(spec: CollectionSpec) -> ClaimsConfig:
    if spec.claims is None:
        raise OperationRejected("This collection has no claims", "claims_disabled")
    if not spec.claims.standalone:
        raise OperationRejected(
            "Claims on this collection are taken by state transitions, not records.claim",
            "claims_not_standalone",
        )
    return spec.claims


def expired(row: sa.RowMapping, at: datetime) -> bool:
    lease = row["lease_expires_at"]
    return lease is not None and datetime.fromisoformat(str(lease)) <= at


def check_holder(row: sa.RowMapping, caller: Mapping[str, JsonValue], epoch: object) -> None:
    """Fencing: only the current holder, at the current epoch, before its lease ends."""
    if row["holder"] != caller["id"] or row["claim_epoch"] != epoch or expired(row, now()):
        raise OperationRejected(
            f"This claim is no longer held at epoch {epoch}; the record is at epoch "
            f"{row['claim_epoch']}",
            "claim_lost",
        )


def check_write(
    spec: CollectionSpec,
    row: sa.RowMapping,
    caller: Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
) -> None:
    """Writes to a held record (decision 2.4, lead 2026-11-09).

    A `claim_epoch` in the call is always checked. While the record is held and the
    collection fences writes (the default), only the holder writes, and only with the
    current epoch: anyone else gets `record_claimed`, the holder without it `claim_lost`.
    The holder of a lapsed claim the sweep has not reached yet is stale. Unheld records
    behave as without claims.
    """
    if "claim_epoch" in arguments:
        check_holder(row, caller, arguments["claim_epoch"])
        return
    if row["holder"] is None or spec.claims is None or not spec.claims.fence_writes:
        return
    if row["holder"] == caller["id"]:
        raise OperationRejected(
            f"Writes to a held record need claim_epoch {row['claim_epoch']}", "claim_lost"
        )
    if not expired(row, now()):
        raise OperationRejected("Another principal holds this record", "record_claimed")


def write_claim(
    conn: sa.Connection,
    row: sa.RowMapping,
    new: Mapping[str, JsonValue],
    claim_transition: str | None = None,
    *,
    holder_event: bool = True,
) -> None:
    """Store a claim or state change; any holder event restarts the follow-up count.

    `claim_transition` names the transition that acquired the claim (its
    `on_claim_expiry` applies later); a claim taken by `records.claim` has none.
    """
    changed = conn.exec_driver_sql(
        "UPDATE records SET state=?,holder=?,claim_epoch=?,lease_expires_at=?,"
        "followups=CASE WHEN ? THEN 0 ELSE followups END,"
        "claim_transition=?,version=?,updated_at=? WHERE seq=? AND version=?",
        (
            new["state"],
            new["holder"],
            new["claim_epoch"],
            new["lease_expires_at"],
            int(holder_event),
            claim_transition if new["holder"] is not None else None,
            new["version"],
            new["updated_at"],
            row["seq"],
            row["version"],
        ),
    ).rowcount
    assert changed == 1


def reset_followups(conn: sa.Connection, row: sa.RowMapping) -> None:
    conn.exec_driver_sql("UPDATE records SET followups=0 WHERE seq=?", (row["seq"],))


def renew(conn: sa.Connection, row: sa.RowMapping, until: datetime) -> dict[str, JsonValue]:
    """Extend a live claim to at least `until`. A renewal changes no version or history."""
    lease = max(datetime.fromisoformat(str(row["lease_expires_at"])), until)
    conn.exec_driver_sql(
        "UPDATE records SET lease_expires_at=?,followups=0 WHERE seq=?", (stamp(lease), row["seq"])
    )
    return {**record_view(row), "lease_expires_at": stamp(lease)}


def expire(
    conn: sa.Connection,
    row: sa.RowMapping,
    at: datetime,
    *,
    actor: str,
    reason: str,
    spec: "CollectionSpec | None" = None,
) -> None:
    """Return a lapsed claim to the pool: holder cleared, epoch incremented, and the state
    set to the acquiring transition's `on_claim_expiry`, with history and a journal event,
    all in the caller's transaction."""
    old = record_view(row)
    acquired = (
        spec.transition(row["claim_transition"]) if spec and row["claim_transition"] else None
    )
    state = acquired.on_claim_expiry if acquired and acquired.on_claim_expiry else old["state"]
    new: dict[str, JsonValue] = {
        **old,
        "state": state,
        "holder": None,
        "lease_expires_at": None,
        "claim_epoch": int(row["claim_epoch"]) + 1,
        "version": int(row["version"]) + 1,
        "updated_at": at.isoformat(),
    }
    write_claim(conn, row, new)
    detail: dict[str, JsonValue] = {
        "reason": reason,
        "previous_holder": row["holder"],
        "previous_epoch": row["claim_epoch"],
        "followups": row["followups"],
        "from": old["state"],
        "to": state,
    }
    namespace = str(row["namespace"])
    append_history(conn, namespace, "claim_expired", actor, None, None, old, new, detail)
    journal(conn, namespace, "claim_expired", new, actor, None, at, detail)


class Records:
    """The built-in record operations, bound to the service database."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.handlers: dict[str, Handler] = {
            "records.create": self.create,
            "records.get": self.get,
            "records.query": self.query,
            "records.update": self.update,
            "records.claim": self.claim,
            "records.release": self.release,
            "records.claimed": self.claimed,
            "records.transition": self.transition,
        }

    def handler(self, name: str) -> Handler:
        return self.handlers[name]

    # Shared checks, inside the call's own transaction

    def _authorize(
        self, conn: sa.Connection, context: OperationContext, collection: str, action: str
    ) -> tuple[CollectionSpec, dict[str, JsonValue]]:
        """Live ceiling on `record:<collection>/<action>`, then the collection document.

        Returns the spec and the guard's `caller`; `group` and `pool` are null unless the
        caller is an agent-group member.
        """
        policy = Policy(conn)
        try:
            binding = policy.ceiling(context.task_id, record_grant(collection, action))
        except Fault as denied:
            raise OperationRejected(denied.message, denied.code) from None
        row = _row(conn, context.namespace, collection)
        if row is None:
            raise OperationRejected("No such collection in this namespace", "collection_not_found")
        spec = CollectionSpec.model_validate_json(row["spec_json"])
        return spec, caller_of(conn, str(binding["agent_id"]))

    @staticmethod
    def _fence(conn: sa.Connection, context: OperationContext) -> None:
        """Write only while this call's receipt is still running.

        Recovery marks an interrupted call uncertain in its own transaction; both are
        serialized by SQLite, so a write either lands while the receipt runs (and the
        reconciler then finds it) or not at all.
        """
        state = conn.exec_driver_sql(
            "SELECT state FROM operations WHERE receipt_id=?", (context.receipt_id,)
        ).scalar()
        if state != "running":
            raise OperationRejected("This call is no longer running", "stale_call")

    @staticmethod
    def _event(
        conn: sa.Connection,
        context: OperationContext,
        caller: str,
        kind: str,
        old: Mapping[str, JsonValue] | None,
        new: Mapping[str, JsonValue],
        detail: Mapping[str, JsonValue] | None = None,
    ) -> None:
        append_history(
            conn,
            context.namespace,
            kind,
            caller,
            context.task_id,
            context.receipt_id,
            old,
            new,
            detail,
        )

    # Operations

    async def create(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        collection = str(arguments["collection"])
        with self.database.engine.begin() as conn:
            spec, caller = self._authorize(conn, context, collection, "create")
            at = now().isoformat()
            record: dict[str, JsonValue] = {
                "collection": collection,
                "record_id": "rec-" + uuid4().hex,
                "data": _data(spec, arguments["data"]),
                "labels": _labels(arguments.get("labels", [])),
                "links": _links(arguments.get("links", [])),
                # A record starts in its collection's initial state (decision 3).
                "state": spec.initial,
                "holder": None,
                "claim_epoch": 0,
                "lease_expires_at": None,
                "version": 1,
                "created_by": caller["id"],
                "created_at": at,
                "updated_at": at,
            }
            check_guard(spec, "create", record, arguments, caller)
            self._fence(conn, context)
            conn.exec_driver_sql(
                "INSERT INTO records(namespace,collection,record_id,data_json,labels_json,"
                "links_json,state,version,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                (
                    context.namespace,
                    collection,
                    record["record_id"],
                    json.dumps(record["data"]),
                    json.dumps(record["labels"]),
                    json.dumps(record["links"]),
                    record["state"],
                    caller["id"],
                    at,
                    at,
                ),
            )
            self._event(conn, context, str(caller["id"]), "created", None, record)
        return {"record": record}

    async def get(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        collection = str(arguments["collection"])
        with self.database.read() as conn:
            self._authorize(conn, context, collection, "read")
            row = _record_row(conn, context.namespace, collection, str(arguments["record_id"]))
            if row is None:
                raise OperationRejected("No such record", "record_not_found")
            return {"record": record_view(row)}

    async def query(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        collection = str(arguments["collection"])
        clauses = ["namespace=?", "collection=?"]
        values: list[Any] = [context.namespace, collection]
        for name in ("state", "holder"):
            if name in arguments:
                clauses.append(f"{name} IS ?")
                values.append(arguments[name])
        if "label" in arguments:
            clauses.append("EXISTS (SELECT 1 FROM json_each(labels_json) WHERE value=?)")
            values.append(arguments["label"])
        where = arguments.get("where", {})
        assert isinstance(where, dict)
        for field, value in sorted(where.items()):
            # Field names are schema-checked identifiers; the path is still a parameter.
            clauses.append("json_extract(data_json, ?) IS ?")
            values.extend((f'$."{field}"', value))
        cursor = arguments.get("cursor")
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise OperationRejected("cursor is not one this operation returned", "bad_cursor")
            clauses.append("seq>?")
            values.append(int(cursor))
        limit = int(arguments.get("limit", 20))  # type: ignore[arg-type]
        with self.database.read() as conn:
            self._authorize(conn, context, collection, "read")
            rows = (
                conn.exec_driver_sql(
                    f"SELECT * FROM records WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
                    (*values, limit + 1),
                )
                .mappings()
                .all()
            )
        page: list[JsonValue] = []
        size = 0
        last: int | None = None
        more = len(rows) > limit
        for row in rows[:limit]:
            view = record_view(row)
            size += len(rfc8785.dumps(view)) + 1
            if size > MAX_PAGE_BYTES and page:
                more = True
                break
            page.append(view)
            last = int(row["seq"])
        return {"records": page, "next_cursor": str(last) if more and last is not None else None}

    async def update(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        collection = str(arguments["collection"])
        record_id = str(arguments["record_id"])
        with self.database.engine.begin() as conn:
            spec, caller = self._authorize(conn, context, collection, "update")
            row = _record_row(conn, context.namespace, collection, record_id)
            if row is None:
                raise OperationRejected("No such record", "record_not_found")
            old = record_view(row)
            expected = arguments["expected_version"]
            if expected != row["version"]:
                raise OperationRejected(
                    f"expected version {expected}, record is at version {row['version']}",
                    "version_conflict",
                )
            check_write(spec, row, caller, arguments)
            check_guard(spec, "update", old, arguments, caller)
            new: dict[str, JsonValue] = {
                **old,
                "data": _data(spec, arguments["data"]) if "data" in arguments else old["data"],
                "labels": _labels(arguments["labels"]) if "labels" in arguments else old["labels"],
                "links": _links(arguments["links"]) if "links" in arguments else old["links"],
                "version": int(row["version"]) + 1,
                "updated_at": now().isoformat(),
            }
            self._fence(conn, context)
            changed = conn.exec_driver_sql(
                "UPDATE records SET data_json=?,labels_json=?,links_json=?,version=?,updated_at=? "
                "WHERE namespace=? AND collection=? AND record_id=? AND version=?",
                (
                    json.dumps(new["data"]),
                    json.dumps(new["labels"]),
                    json.dumps(new["links"]),
                    new["version"],
                    new["updated_at"],
                    context.namespace,
                    collection,
                    record_id,
                    row["version"],
                ),
            ).rowcount
            # BEGIN IMMEDIATE holds the write lock, so the version cannot move under us.
            assert changed == 1
            if row["holder"] == caller["id"]:
                # The holder touched its record: follow-up wakes start counting again.
                reset_followups(conn, row)
            self._event(conn, context, str(caller["id"]), "updated", old, new)
        return {"record": new}

    async def claim(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        """Atomic standalone claim (decision 2.4). One BEGIN IMMEDIATE transaction re-reads
        the record and checks it is free, the guard and the holder's limit, then takes it;
        SQLite's single writer lets exactly one of several concurrent claims win."""
        collection = str(arguments["collection"])
        record_id = str(arguments["record_id"])
        with self.database.engine.begin() as conn:
            spec, caller = self._authorize(conn, context, collection, "claim")
            claims = standalone(spec)
            row = _record_row(conn, context.namespace, collection, record_id)
            if row is None:
                raise OperationRejected("No such record", "record_not_found")
            at = now()
            holder = str(caller["id"])
            self._fence(conn, context)
            if row["holder"] is not None and expired(row, at):
                # A lapsed claim the sweep has not reached yet is released first.
                expire(conn, row, at, actor=holder, reason="lease_expired", spec=spec)
                row = _record_row(conn, context.namespace, collection, record_id)
                assert row is not None
            if row["holder"] == holder:
                # An idempotent re-claim is evidence the holder is alive: it renews.
                renewed = renew(conn, row, at + timedelta(seconds=claims.lease_seconds))
                return {"record": renewed, "claim": claim_view(renewed)}
            if row["holder"] is not None:
                raise OperationRejected("Another principal holds this record", "record_claimed")
            old = record_view(row)
            check_guard(spec, "claim", old, arguments, caller)
            held = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM records WHERE namespace=? AND collection=? AND holder=? "
                "AND lease_expires_at>?",
                (context.namespace, collection, holder, stamp(at)),
            ).scalar_one()
            if held >= claims.max_per_holder:
                raise OperationRejected(
                    f"The holder already has {held} claim(s); the collection allows "
                    f"{claims.max_per_holder}",
                    "claim_limit",
                )
            new: dict[str, JsonValue] = {
                **old,
                "holder": holder,
                "claim_epoch": int(row["claim_epoch"]) + 1,
                "lease_expires_at": stamp(at + timedelta(seconds=claims.lease_seconds)),
                "version": int(row["version"]) + 1,
                "updated_at": at.isoformat(),
            }
            write_claim(conn, row, new)
            self._event(conn, context, holder, "claimed", old, new)
            journal(conn, context.namespace, "claimed", new, holder, context.task_id, at, {})
        return {"record": new, "claim": claim_view(new)}

    async def release(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        """The holder gives the record back; a stale holder is refused with `claim_lost`."""
        collection = str(arguments["collection"])
        record_id = str(arguments["record_id"])
        with self.database.engine.begin() as conn:
            spec, caller = self._authorize(conn, context, collection, "claim")
            standalone(spec)
            row = _record_row(conn, context.namespace, collection, record_id)
            if row is None:
                raise OperationRejected("No such record", "record_not_found")
            check_holder(row, caller, arguments["claim_epoch"])
            old = record_view(row)
            check_guard(spec, "release", old, arguments, caller)
            new: dict[str, JsonValue] = {
                **old,
                "holder": None,
                "lease_expires_at": None,
                "version": int(row["version"]) + 1,
                "updated_at": now().isoformat(),
            }
            self._fence(conn, context)
            write_claim(conn, row, new)
            self._event(conn, context, str(caller["id"]), "released", old, new)
            journal(
                conn,
                context.namespace,
                "released",
                new,
                str(caller["id"]),
                context.task_id,
                now(),
                {"previous_holder": caller["id"], "previous_epoch": row["claim_epoch"]},
            )
        return {"record": new}

    async def claimed(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        """The caller's live claims in one collection, with fencing epoch and the
        idempotency context to use for effects taken for each record."""
        collection = str(arguments["collection"])
        with self.database.read() as conn:
            _, caller = self._authorize(conn, context, collection, "read")
            rows = (
                conn.exec_driver_sql(
                    "SELECT * FROM records WHERE namespace=? AND collection=? AND holder=? "
                    "AND lease_expires_at>? ORDER BY seq LIMIT 64",
                    (context.namespace, collection, caller["id"], stamp(now())),
                )
                .mappings()
                .all()
            )
        claims: list[JsonValue] = []
        size = 0
        for row in rows:
            view = record_view(row)
            size += len(rfc8785.dumps(view)) + 256
            if size > MAX_PAGE_BYTES and claims:
                break
            claims.append({**claim_view(view), "record": view})
        return {"claims": claims}

    async def transition(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> dict[str, JsonValue]:
        """Move a record along its collection's table (decision 3.1), in one transaction:
        the live grant `record:<C>/<transition>`, the from state, the guard, the claim
        effect and the receipt evidence; then the state, history and journal event."""
        collection = str(arguments["collection"])
        record_id = str(arguments["record_id"])
        name = str(arguments["transition"])
        targs = arguments.get("args", {})
        assert isinstance(targs, dict)
        with self.database.engine.begin() as conn:
            spec, caller = self._authorize(conn, context, collection, name)
            transition = spec.transition(name)
            if transition is None:
                raise OperationRejected(
                    f"Collection {collection} has no transition {name}", "transition_unknown"
                )
            row = _record_row(conn, context.namespace, collection, record_id)
            if row is None:
                raise OperationRejected("No such record", "record_not_found")
            at = now()
            actor = str(caller["id"])
            self._fence(conn, context)
            if row["holder"] is not None and expired(row, at):
                # A lapsed claim is released first, as the sweep would, state included.
                expire(conn, row, at, actor=actor, reason="lease_expired", spec=spec)
                row = _record_row(conn, context.namespace, collection, record_id)
                assert row is not None
            if row["state"] not in transition.from_:
                raise OperationRejected(
                    f"Transition {name} needs state {' or '.join(transition.from_)}; the "
                    f"record is in {row['state']}",
                    "transition_not_allowed",
                )
            old = record_view(row)
            check_condition(transition.guard, name, old, targs, caller)
            new: dict[str, JsonValue] = {
                **old,
                "state": transition.to,
                "version": int(row["version"]) + 1,
                "updated_at": at.isoformat(),
            }
            acquired = row["claim_transition"]
            if transition.claim == "acquire":
                assert spec.claims is not None, "checked at apply"
                if row["holder"] is not None and row["holder"] != actor:
                    raise OperationRejected("Another principal holds this record", "record_claimed")
                if row["holder"] == actor:
                    if "claim_epoch" in arguments:
                        check_holder(row, caller, arguments["claim_epoch"])
                else:
                    self._check_limit(conn, context.namespace, collection, actor, spec, at)
                    new["claim_epoch"] = int(row["claim_epoch"]) + 1
                new["holder"] = actor
                new["lease_expires_at"] = stamp(at + timedelta(seconds=spec.claims.lease_seconds))
                acquired = name
            elif transition.claim == "holder":
                check_holder(row, caller, arguments.get("claim_epoch"))
            elif transition.claim == "release":
                # The holder releases with its epoch; another principal may close the work
                # when its grant and the guard allow, e.g. a reviewer approving it.
                if row["holder"] == actor:
                    check_holder(row, caller, arguments.get("claim_epoch"))
                new["holder"] = None
                new["lease_expires_at"] = None
            receipt = None
            if transition.requires_receipt is not None:
                receipt = check_receipt(
                    conn, transition.requires_receipt, arguments, context, actor, record_id
                )
            write_claim(
                conn, row, new, acquired, holder_event=actor in (row["holder"], new["holder"])
            )
            detail: dict[str, JsonValue] = {
                "transition": name,
                "from": old["state"],
                "to": transition.to,
                "claim": transition.claim,
                "args": targs,
                "receipt_id": receipt,
            }
            self._event(conn, context, actor, "transitioned", old, new, detail)
            journal(
                conn,
                context.namespace,
                "transitioned",
                new,
                actor,
                context.task_id,
                at,
                {"from": old["state"], "to": transition.to, "transition": name},
            )
        result: dict[str, JsonValue] = {"record": new}
        if new["holder"] is not None:
            result["claim"] = claim_view(new)
        return result

    @staticmethod
    def _check_limit(
        conn: sa.Connection,
        namespace: str,
        collection: str,
        holder: str,
        spec: CollectionSpec,
        at: datetime,
    ) -> None:
        assert spec.claims is not None
        held = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM records WHERE namespace=? AND collection=? AND holder=? "
            "AND lease_expires_at>?",
            (namespace, collection, holder, stamp(at)),
        ).scalar_one()
        if held >= spec.claims.max_per_holder:
            raise OperationRejected(
                f"The holder already has {held} claim(s); the collection allows "
                f"{spec.claims.max_per_holder}",
                "claim_limit",
            )

    async def reconcile(
        self, arguments: dict[str, JsonValue], context: OperationContext
    ) -> Reconciliation:
        """An interrupted create or update applied exactly when its history row exists."""
        with self.database.read() as conn:
            row = conn.exec_driver_sql(
                "SELECT new_json FROM record_events WHERE receipt_id=?", (context.receipt_id,)
            ).first()
        if row is None:
            return Reconciliation(applied=False)
        return Reconciliation(applied=True, output={"record": json.loads(row[0])})

    def reconciler(self, name: str) -> Reconciler | None:
        return self.reconcile if name in WRITES else None


# Built-in descriptors

_COLLECTION: dict[str, JsonValue] = {"type": "string", "pattern": f"^{COLLECTION_NAME}$"}
_RECORD_ID: dict[str, JsonValue] = {"type": "string", "pattern": "^rec-[0-9a-f]{32}$"}
_LABEL: dict[str, JsonValue] = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,63}$"}
_LINK: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "rel": {"type": "string", "minLength": 1, "maxLength": 64},
        "url": {"type": "string", "minLength": 1, "maxLength": 2048},
        "external_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "observed_state": {"type": "string", "maxLength": 64},
        "observed_at": {"type": "string", "format": "date-time"},
    },
    "required": ["rel"],
    "additionalProperties": False,
}
_LABELS_SCHEMA: dict[str, JsonValue] = {
    "type": "array",
    "items": _LABEL,
    "maxItems": MAX_LABELS,
    "uniqueItems": True,
}
_LINKS_SCHEMA: dict[str, JsonValue] = {"type": "array", "items": _LINK, "maxItems": MAX_LINKS}
_SCALAR: dict[str, JsonValue] = {"type": ["string", "number", "boolean", "null"]}
_RECORD: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "collection": {"type": "string"},
        "record_id": {"type": "string"},
        "data": {"type": "object"},
        "labels": {"type": "array", "items": {"type": "string"}},
        "links": {"type": "array", "items": {"type": "object"}},
        "state": {"type": ["string", "null"]},
        "holder": {"type": ["string", "null"]},
        "claim_epoch": {"type": "integer"},
        "lease_expires_at": {"type": ["string", "null"]},
        "version": {"type": "integer", "minimum": 1},
        "created_by": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
    },
    "required": ["collection", "record_id", "data", "labels", "links", "version"],
}
_EPOCH: dict[str, JsonValue] = {"type": "integer", "minimum": 1}
_CLAIM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "collection": {"type": "string"},
        "record_id": {"type": "string"},
        "claim_epoch": {"type": "integer", "minimum": 1},
        "lease_expires_at": {"type": "string"},
        "idempotency_context": {"type": "string"},
    },
    "required": ["collection", "record_id", "claim_epoch", "lease_expires_at"],
}
_ONE: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"record": _RECORD},
    "required": ["record"],
}


def _descriptor(
    name: str,
    title: str,
    effect: Literal["read", "effect"],
    properties: dict[str, JsonValue],
    required: list[JsonValue],
    output: dict[str, JsonValue],
    **binding: JsonValue,
) -> OperationDescriptor:
    return OperationDescriptor.model_validate(
        {
            "tool": {
                "name": name,
                "title": title,
                "description": title + ". Needs the grant record:<collection>/<action>.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"collection": _COLLECTION, **properties},
                    "required": ["collection", *required],
                    "additionalProperties": False,
                },
                "outputSchema": output,
            },
            "cardea": {
                "version": 1,
                "effect": effect,
                "handler": "cardea." + name,
                "timeout_seconds": 10,
                **binding,
            },
        },
        context={"builtin": True},
    )


DESCRIPTORS: tuple[OperationDescriptor, ...] = (
    _descriptor(
        "records.create",
        "Create one record in a collection (grant action: create)",
        "effect",
        {
            # Retrying with the same key in the same idempotency context returns the
            # first record instead of creating another (ADR-026).
            "client_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "data": {"type": "object"},
            "labels": _LABELS_SCHEMA,
            "links": _LINKS_SCHEMA,
        },
        ["client_key", "data"],
        _ONE,
        idempotency_scope=["/collection", "/client_key"],
        reconciler="cardea.records.reconcile",
    ),
    _descriptor(
        "records.get",
        "Read one record (grant action: read)",
        "read",
        {"record_id": _RECORD_ID},
        ["record_id"],
        _ONE,
    ),
    _descriptor(
        "records.query",
        "List records by state, label, holder and data field equality, paged (grant action: read)",
        "read",
        {
            "state": {"type": ["string", "null"], "maxLength": 64},
            "label": _LABEL,
            "holder": {"type": ["string", "null"], "maxLength": 128},
            "where": {
                "type": "object",
                "propertyNames": {"pattern": FIELD},
                "additionalProperties": _SCALAR,
                "maxProperties": 8,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_LIMIT},
            "cursor": {"type": "string", "pattern": "^[0-9]{1,19}$"},
        },
        [],
        {
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": _RECORD},
                "next_cursor": {"type": ["string", "null"]},
            },
            "required": ["records", "next_cursor"],
        },
    ),
    _descriptor(
        "records.update",
        "Replace a record's data, labels or links if it is still at expected_version "
        "(grant action: update)",
        "effect",
        {
            "record_id": _RECORD_ID,
            "expected_version": {"type": "integer", "minimum": 1},
            "data": {"type": "object"},
            "labels": _LABELS_SCHEMA,
            "links": _LINKS_SCHEMA,
            # Fencing: required while the record is held (unless the collection sets
            # claims.fence_writes false); refused with claim_lost unless the caller still
            # holds the record at this epoch.
            "claim_epoch": _EPOCH,
        },
        ["record_id", "expected_version"],
        _ONE,
        reconciler="cardea.records.reconcile",
    ),
    _descriptor(
        "records.claim",
        "Claim a record for the caller, or renew the caller's claim "
        "(grant action: claim; collections with standalone claims)",
        "effect",
        {"record_id": _RECORD_ID},
        ["record_id"],
        {
            "type": "object",
            "properties": {"record": _RECORD, "claim": _CLAIM},
            "required": ["record", "claim"],
        },
        reconciler="cardea.records.reconcile",
    ),
    _descriptor(
        "records.release",
        "Give back a record the caller holds at claim_epoch (grant action: claim)",
        "effect",
        {"record_id": _RECORD_ID, "claim_epoch": _EPOCH},
        ["record_id", "claim_epoch"],
        _ONE,
        reconciler="cardea.records.reconcile",
    ),
    _descriptor(
        "records.transition",
        "Move a record along its collection's state machine (grant action: the transition's name)",
        "effect",
        {
            "record_id": _RECORD_ID,
            "transition": {"type": "string", "pattern": f"^{RECORD_ACTION}$"},
            # Needed by claim: holder and by the holder for claim: release.
            "claim_epoch": _EPOCH,
            # A receipt of the operation the transition requires, when it requires one.
            "receipt_id": {"type": "string", "minLength": 1, "maxLength": 128},
            # The transition's own arguments; guards see them as `args`.
            "args": {"type": "object", "maxProperties": 32},
        },
        ["record_id", "transition"],
        {
            "type": "object",
            "properties": {"record": _RECORD, "claim": _CLAIM},
            "required": ["record"],
        },
        reconciler="cardea.records.reconcile",
    ),
    _descriptor(
        "records.claimed",
        "List the caller's live claims in a collection, with epoch, lease and the "
        "idempotency context for effects (grant action: read)",
        "read",
        {},
        [],
        {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        **_CLAIM,
                        "properties": {**_CLAIM["properties"], "record": _RECORD},
                    },
                }
            },
            "required": ["claims"],
        },
    ),
)
