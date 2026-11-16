"""Who disabled a group member's principal (ADR-034 decision 1.5, follow-up to slice 8).

`group_members.disabled_by_drain` is 1 when draining disabled the principal, so scaling up
or reviving the group re-enables it. A principal an operator disabled (for example as a
kill switch) has 0 and stays disabled.

Members removed before this revision were disabled by the drain, the only automatic path
that disables a member, unless their principal was already disabled when they drained;
that history was not recorded, so they are backfilled as drain-disabled.
"""

from alembic import op

revision = "m16_member_disable"
down_revision = "m15_spend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE group_members ADD COLUMN disabled_by_drain INTEGER NOT NULL DEFAULT 0 "
        "CHECK(disabled_by_drain IN (0,1))"
    )
    op.execute("UPDATE group_members SET disabled_by_drain=1 WHERE state='removed'")


def downgrade() -> None:
    raise RuntimeError("Member disable reasons cannot be downgraded; restore a verified backup")
