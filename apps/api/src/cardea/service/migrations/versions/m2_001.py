"""Host allocations share the existing receipt transaction; preserve all prior history."""

from alembic import op

revision = "m2_001"
down_revision = "m1_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE host_budget_limits (
      principal_id TEXT NOT NULL REFERENCES principals(principal_id), namespace TEXT NOT NULL,
      tool_ms INTEGER NOT NULL CHECK(tool_ms>=0),
      output_bytes INTEGER NOT NULL CHECK(output_bytes>=0),
      concurrency INTEGER NOT NULL CHECK(concurrency>=1), PRIMARY KEY(principal_id,namespace))""")
    op.execute("""CREATE TABLE host_reservations (
      task_id TEXT PRIMARY KEY REFERENCES task_bindings(task_id),
      principal_id TEXT NOT NULL REFERENCES principals(principal_id), namespace TEXT NOT NULL,
      tool_ms INTEGER NOT NULL CHECK(tool_ms BETWEEN 0 AND 10000),
      output_bytes INTEGER NOT NULL CHECK(output_bytes BETWEEN 0 AND 65536),
      settled INTEGER NOT NULL CHECK(settled IN (0,1)), cleaned INTEGER NOT NULL DEFAULT 0
      CHECK(cleaned IN (0,1)))""")
    op.execute(
        """CREATE INDEX host_reservation_account ON host_reservations(principal_id,namespace)"""
    )
    # Historical host effects are charged conservatively, never granted fresh capacity on upgrade.
    op.execute("""INSERT INTO host_reservations
      SELECT b.task_id,b.supervisor_id,b.namespace,10000,65536,
        CASE WHEN t.state IN ('succeeded','failed','cancelled') THEN 1 ELSE 0 END,0
      FROM task_bindings b JOIN tasks t USING(task_id)
      JOIN operations o ON o.task_id=b.task_id AND o.operation_id='execute'
      WHERE json_extract(t.snapshot,'$.runtime_id')='host-sha256-v1'""")


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; budget history cannot be discarded")
