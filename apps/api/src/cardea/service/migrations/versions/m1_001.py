"""Add authenticated provenance without rewriting the legacy schema or its hashes."""

from alembic import op

revision = "m1_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in (
        """CREATE TABLE principals (
          principal_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
          disabled INTEGER NOT NULL CHECK(disabled IN (0,1)),
          auth_kind TEXT, issuer TEXT NOT NULL, subject TEXT NOT NULL,
          runtime_id TEXT REFERENCES principals(principal_id),
          UNIQUE(auth_kind, issuer, subject))""",
        """CREATE TABLE policies (
          principal_id TEXT NOT NULL, namespace TEXT NOT NULL,
          operation TEXT NOT NULL, mode TEXT NOT NULL, expires_at REAL NOT NULL,
          PRIMARY KEY(principal_id,namespace,operation,mode))""",
        """CREATE TABLE task_bindings (
          task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
          supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
          agent_id TEXT NOT NULL REFERENCES principals(principal_id),
          runtime_id TEXT NOT NULL REFERENCES principals(principal_id),
          namespace TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
          request_json TEXT NOT NULL, grants_json TEXT NOT NULL,
          admission_cursor INTEGER NOT NULL, parent_task_id TEXT REFERENCES task_bindings(task_id),
          UNIQUE(supervisor_id,request_id))""",
        """CREATE TRIGGER provenance_immutable BEFORE UPDATE ON task_bindings
          BEGIN SELECT RAISE(ABORT, 'task provenance is immutable'); END""",
        """CREATE TABLE service_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        "INSERT INTO service_meta VALUES ('policy_revision', '0')",
        """CREATE TABLE control_requests (
          task_id TEXT NOT NULL REFERENCES task_bindings(task_id), request_id TEXT NOT NULL,
          kind TEXT NOT NULL, cursor INTEGER NOT NULL, PRIMARY KEY(task_id,request_id))""",
    ):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; authenticated provenance cannot be downgraded")
