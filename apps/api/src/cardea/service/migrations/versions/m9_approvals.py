"""Approval requests, their decisions and host approval rules (ADR-036 slice 2)."""

from alembic import op

revision = "m9_approvals"
down_revision = "m8_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Host approval rules per namespace and operation, replaced as a whole by provisioning.
    op.execute("""CREATE TABLE approval_rules (
      namespace TEXT NOT NULL, operation TEXT NOT NULL, rules_json TEXT NOT NULL,
      PRIMARY KEY(namespace, operation))""")
    # One frozen intent waiting for, or past, a decision. The arguments are the validated
    # document the task was admitted with; the digests bind what an approver decides.
    op.execute("""CREATE TABLE approval_requests (
      request_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES task_bindings(task_id),
      namespace TEXT NOT NULL,
      supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
      agent_id TEXT NOT NULL REFERENCES principals(principal_id),
      runtime_id TEXT NOT NULL REFERENCES principals(principal_id),
      operation TEXT NOT NULL,
      operation_version INTEGER NOT NULL,
      operation_id TEXT NOT NULL,
      arguments_json TEXT NOT NULL,
      argument_sha256 TEXT NOT NULL,
      idempotency_context TEXT,
      scope_key TEXT,
      intent_digest TEXT NOT NULL,
      rules_json TEXT NOT NULL,
      rules_digest TEXT NOT NULL,
      distinct_from_supervisor INTEGER NOT NULL CHECK(distinct_from_supervisor IN (0,1)),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      on_expiry TEXT NOT NULL CHECK(on_expiry IN ('resume','fail')),
      state TEXT NOT NULL CHECK(state IN
        ('pending','approved','executed','rejected','expired','withdrawn','void')),
      void_reason TEXT,
      receipt_id TEXT,
      updated_at TEXT NOT NULL,
      UNIQUE(task_id, operation_id),
      CHECK((state = 'void') = (void_reason IS NOT NULL)),
      CHECK((state = 'executed') = (receipt_id IS NOT NULL)))""")
    # At most one open request per task (ADR-036 decision 3).
    op.execute(
        "CREATE UNIQUE INDEX approval_open_per_task ON approval_requests(task_id) "
        "WHERE state IN ('pending','approved')"
    )
    op.execute(
        "CREATE INDEX approval_requests_by_state ON approval_requests(namespace, state, request_id)"
    )
    # The frozen intent never changes; only its state and outcome columns do.
    op.execute("""CREATE TRIGGER approval_intent_immutable
      BEFORE UPDATE OF request_id, task_id, namespace, supervisor_id, agent_id, runtime_id,
        operation, operation_version, operation_id, arguments_json, argument_sha256,
        idempotency_context, scope_key, intent_digest, rules_json, rules_digest,
        distinct_from_supervisor, created_at, expires_at, on_expiry
      ON approval_requests
      BEGIN SELECT RAISE(ABORT, 'an approval intent is immutable'); END""")
    # One decision per request; the first commit wins. `body_sha256` makes a replayed
    # decision_id with a different body a conflict.
    op.execute("""CREATE TABLE approval_decisions (
      decision_id TEXT PRIMARY KEY,
      request_id TEXT NOT NULL UNIQUE REFERENCES approval_requests(request_id),
      approver_id TEXT NOT NULL REFERENCES principals(principal_id),
      decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
      reason TEXT,
      intent_digest TEXT NOT NULL,
      body_sha256 TEXT NOT NULL,
      decided_at TEXT NOT NULL)""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER approval_decisions_no_{action.lower()}
          BEFORE {action} ON approval_decisions
          BEGIN SELECT RAISE(ABORT, 'approval decisions are append-only'); END""")


def downgrade() -> None:
    raise RuntimeError("Approval requests cannot be downgraded; restore a verified backup")
