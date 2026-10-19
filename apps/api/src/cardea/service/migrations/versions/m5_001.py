"""Append-only recovery generations prevent reuse of historical execution scopes."""

from alembic import op

revision = "m5_001"
down_revision = "m4_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE recovery_epochs (
      epoch TEXT PRIMARY KEY, restored_at TEXT NOT NULL, source_sha256 TEXT NOT NULL)""")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER recovery_epochs_no_{action.lower()}
          BEFORE {action} ON recovery_epochs
          BEGIN SELECT RAISE(ABORT, 'recovery epochs are immutable'); END""")


def downgrade() -> None:
    raise RuntimeError("Recovery fences cannot be downgraded")
