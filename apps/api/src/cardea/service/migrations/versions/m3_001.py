"""Durable agent rounds, input deliveries and provider/effect allocations."""

from alembic import op

revision = "m3_001"
down_revision = "m2_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE model_budget_limits (
      principal_id TEXT NOT NULL REFERENCES principals(principal_id), namespace TEXT NOT NULL,
      calls INTEGER NOT NULL CHECK(calls>=0), tokens INTEGER NOT NULL CHECK(tokens>=0),
      PRIMARY KEY(principal_id,namespace))""")
    op.execute("""CREATE TABLE agent_inputs (
      task_id TEXT NOT NULL REFERENCES task_bindings(task_id), round INTEGER NOT NULL,
      request_id TEXT NOT NULL, digest TEXT NOT NULL,
      content TEXT NOT NULL, cursor INTEGER NOT NULL,
      PRIMARY KEY(task_id,round), UNIQUE(task_id,request_id))""")
    op.execute("""CREATE TABLE agent_runs (
      task_id TEXT PRIMARY KEY REFERENCES task_bindings(task_id), round INTEGER NOT NULL DEFAULT 0,
      stage TEXT NOT NULL DEFAULT 'ready', proposal TEXT, result TEXT)""")
    op.execute("""CREATE TABLE agent_effects (
      task_id TEXT NOT NULL REFERENCES task_bindings(task_id), operation_id TEXT NOT NULL,
      principal_id TEXT NOT NULL REFERENCES principals(principal_id), namespace TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('model','tool')), job TEXT UNIQUE,
      calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
      tool_ms INTEGER NOT NULL DEFAULT 0, output_bytes INTEGER NOT NULL DEFAULT 0,
      settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1)),
      cleaned INTEGER NOT NULL DEFAULT 0 CHECK(cleaned IN (0,1)),
      PRIMARY KEY(task_id,operation_id))""")
    op.execute("CREATE INDEX agent_effect_account ON agent_effects(principal_id,namespace)")


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; agent effect history cannot be discarded")
