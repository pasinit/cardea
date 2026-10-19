"""Idempotency scopes for installed effect operations (ADR-026)."""

from alembic import op

revision = "m6_001"
down_revision = "m5_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per declared effect scope; the receipt that owns it is the scope's authority.
    op.execute("""CREATE TABLE effect_scopes (
      scope_key TEXT PRIMARY KEY,
      namespace TEXT NOT NULL,
      operation TEXT NOT NULL,
      task_id TEXT NOT NULL,
      operation_id TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','unknown')),
      updated_at TEXT NOT NULL)""")


def downgrade() -> None:
    raise RuntimeError("Effect scopes cannot be downgraded")
