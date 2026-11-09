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
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

import rfc8785
import sqlalchemy as sa
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from cardea.kernel.models import Identifier
from cardea.service.approvals import compile_condition, execute_condition
from cardea.service.database import Database, Fault
from cardea.service.models import (
    COLLECTION_NAME,
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

API_VERSION = "cardea/v1alpha1"
GUARD_VARIABLES = frozenset({"record", "args", "caller"})
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


def _guard(source: str) -> str:
    compile_condition(source, GUARD_VARIABLES)
    return source


GuardSource = Annotated[str, Field(strict=True), AfterValidator(_guard)]


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


def lease_bound(attempts: int, timeout_seconds: int) -> int:
    """The shortest lease a pool that may claim needs: two full runs plus margin."""
    return 2 * attempts * timeout_seconds + LEASE_BOUND_MARGIN_SECONDS


def check_lease(collection: str, claims: "ClaimsConfig | None", pools: Iterable[GroupPool]) -> None:
    """Refuse a lease shorter than `lease_bound` for any pool granted `record:<C>/claim`."""
    if claims is None:
        return
    for pool in pools:
        if record_grant(collection, "claim") not in pool.grants:
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
    """`kind: Collection` (ADR-034 decision 2). State machines are slice 3."""

    apiVersion: Literal["cardea/v1alpha1"] = "cardea/v1alpha1"  # noqa: N815 - Kubernetes shape
    kind: Literal["Collection"] = "Collection"
    metadata: CollectionMetadata
    spec: CollectionSpec

    def spec_json(self) -> dict[str, JsonValue]:
        """The stored spec. Absent later-slice fields stay absent, so a collection applied
        before they existed keeps its digest and generation."""
        spec = self.spec.model_dump(mode="json", by_alias=True)
        if spec.get("claims") is None:
            spec.pop("claims", None)
        guards = spec["guards"]
        assert isinstance(guards, dict)
        for action in ("claim", "release"):
            if guards.get(action) is None:
                guards.pop(action, None)
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
        check_lease(name, document.spec.claims, group.pools)
    if existing is None:
        change, generation = "created", 1
    elif existing["spec_digest"] == digest:
        change, generation = "unchanged", int(existing["generation"])
    else:
        change, generation = "updated", int(existing["generation"]) + 1
    plan = {"change": change, "generation": generation, "dry_run": dry_run}
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
    row = _row(conn, namespace, name)
    assert row is not None
    return {**_document_view(row, _count(conn, namespace, name)), "plan": plan}


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
    """Fail closed: a false guard, or one that errors, refuses the call (ADR-034 2.3)."""
    source = getattr(spec.guards, action, None)
    if source is None:
        return
    value, error = execute_condition(
        source,
        {"record": dict(record), "args": dict(args), "caller": dict(caller)},
        GUARD_VARIABLES,
    )
    if error is not None:
        raise OperationRejected(f"{action} guard failed to evaluate: {error}", "guard_refused")
    if value is not True:
        raise OperationRejected(f"{action} guard refused this call", "guard_refused")


class Records:
    """The built-in record operations, bound to the service database."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.handlers: dict[str, Handler] = {
            "records.create": self.create,
            "records.get": self.get,
            "records.query": self.query,
            "records.update": self.update,
        }

    def handler(self, name: str) -> Handler:
        return self.handlers[name]

    # Shared checks, inside the call's own transaction

    def _authorize(
        self, conn: sa.Connection, context: OperationContext, collection: str, action: str
    ) -> tuple[CollectionSpec, dict[str, JsonValue]]:
        """Live ceiling on `record:<collection>/<action>`, then the collection document.

        Returns the spec and the guard's `caller`. Group and pool are null until agent
        groups exist (slice 4).
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
        caller: dict[str, JsonValue] = {"id": binding["agent_id"], "group": None, "pool": None}
        return spec, caller

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
    ) -> None:
        conn.exec_driver_sql(
            "INSERT INTO record_events(namespace,collection,record_id,version,kind,actor,"
            "task_id,receipt_id,old_json,new_json,at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                context.namespace,
                new["collection"],
                new["record_id"],
                new["version"],
                kind,
                caller,
                context.task_id,
                context.receipt_id,
                json.dumps(old) if old is not None else None,
                json.dumps(new),
                new["updated_at"],
            ),
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
                # Collections with a state machine (slice 3) set the initial state here.
                "state": None,
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
                "links_json,version,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?,?)",
                (
                    context.namespace,
                    collection,
                    record["record_id"],
                    json.dumps(record["data"]),
                    json.dumps(record["labels"]),
                    json.dumps(record["links"]),
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
            self._event(conn, context, str(caller["id"]), "updated", old, new)
        return {"record": new}

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
        return self.reconcile if name in {"records.create", "records.update"} else None


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
        },
        ["record_id", "expected_version"],
        _ONE,
        reconciler="cardea.records.reconcile",
    ),
)
