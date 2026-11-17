"""Run windows for read operations such as `env.try` (ADR-038 decision 5).

`env_runs` holds one row per run key: the SHA-256 of the call's key arguments and its
environment digest. A settled row inside the operation's window answers an identical call
with the earlier result; a running row refuses it (`env_run_in_progress`), so two identical
calls never start two instances. `env_reruns` records each forced rerun, so they can be
limited per commit and day. ADR-026's effect scopes are unchanged.
"""

from alembic import op

revision = "m18_env_runs"
down_revision = "m17_content_redaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE env_runs (
      run_key TEXT PRIMARY KEY CHECK(length(run_key) = 64),
      namespace TEXT NOT NULL,
      operation TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('running','settled')),
      task_id TEXT NOT NULL,
      operation_id TEXT NOT NULL,
      receipt_id TEXT NOT NULL,
      environment_sha256 TEXT NOT NULL,
      started_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      settled_at TEXT
    )""")
    op.execute("CREATE INDEX env_runs_by_call ON env_runs(task_id, operation_id)")
    op.execute("""CREATE TABLE env_reruns (
      rerun_id INTEGER PRIMARY KEY,
      namespace TEXT NOT NULL,
      operation TEXT NOT NULL,
      subject_sha256 TEXT NOT NULL CHECK(length(subject_sha256) = 64),
      receipt_id TEXT NOT NULL,
      at TEXT NOT NULL
    )""")
    op.execute(
        "CREATE INDEX env_reruns_by_subject ON env_reruns(namespace, operation, subject_sha256, at)"
    )


def downgrade() -> None:
    raise RuntimeError("Run windows cannot be downgraded; restore a verified backup")
