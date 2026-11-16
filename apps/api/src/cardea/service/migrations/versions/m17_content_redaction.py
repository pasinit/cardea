"""Task provenance and approval intents stay immutable, except the content gate's
redaction (ADR-037).

A call the content gate refused has its refused content replaced by a marker in the
task's bound request, and, when the call was held for approval and ran as `apr-NN`, in its
approval request. Every other column, the digests included, must stay as it was, and the
task must hold a receipt for that call that failed with `content_refused`.
"""

from alembic import op

revision = "m17_content_redaction"
down_revision = "m16_member_disable"
branch_labels = None
depends_on = None

COLUMNS = (
    "task_id",
    "supervisor_id",
    "agent_id",
    "runtime_id",
    "namespace",
    "request_id",
    "request_digest",
    "grants_json",
    "admission_cursor",
    "parent_task_id",
)


def upgrade() -> None:
    op.execute("DROP TRIGGER provenance_immutable")
    unchanged = " AND ".join(f"NEW.{c} IS OLD.{c}" for c in COLUMNS)
    op.execute(f"""CREATE TRIGGER provenance_immutable BEFORE UPDATE ON task_bindings
      WHEN NOT ({unchanged}
        AND instr(NEW.request_json, '"redacted": "content_refused"') > 0
        AND EXISTS (SELECT 1 FROM operations WHERE task_id = NEW.task_id
          AND json_extract(snapshot, '$.failure.code') = 'content_refused'))
      BEGIN SELECT RAISE(ABORT, 'task provenance is immutable'); END""")
    _intents()


INTENT = (
    "request_id",
    "task_id",
    "namespace",
    "supervisor_id",
    "agent_id",
    "runtime_id",
    "operation",
    "operation_version",
    "operation_id",
    "argument_sha256",
    "idempotency_context",
    "scope_key",
    "intent_digest",
    "rules_json",
    "rules_digest",
    "distinct_from_supervisor",
    "created_at",
    "expires_at",
    "on_expiry",
)


def _intents() -> None:
    op.execute("DROP TRIGGER approval_intent_immutable")
    unchanged = " AND ".join(f"NEW.{c} IS OLD.{c}" for c in INTENT)
    columns = ", ".join((*INTENT, "arguments_json"))
    op.execute(f"""CREATE TRIGGER approval_intent_immutable
      BEFORE UPDATE OF {columns} ON approval_requests
      WHEN NOT ({unchanged}
        AND instr(NEW.arguments_json, '"redacted": "content_refused"') > 0
        AND EXISTS (SELECT 1 FROM operations WHERE task_id = NEW.task_id
          AND operation_id = NEW.operation_id
          AND json_extract(snapshot, '$.failure.code') = 'content_refused'))
      BEGIN SELECT RAISE(ABORT, 'an approval intent is immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("Provenance rules cannot be downgraded; restore a verified backup")
