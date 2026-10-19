"""Supervisor-managed triggers (ADR-029)."""

from alembic import op

revision = "m6_003"
down_revision = "m6_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provisioning replaces only host-owned triggers; supervisors manage their own.
    op.execute(
        "ALTER TABLE triggers ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'host' "
        "CHECK(managed_by IN ('host','supervisor'))"
    )


def downgrade() -> None:
    raise RuntimeError("Trigger ownership cannot be downgraded; restore a verified backup")
