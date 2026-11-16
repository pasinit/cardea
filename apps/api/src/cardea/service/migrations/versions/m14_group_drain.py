"""Draining and deleting agent groups and collections (ADR-034 decision 1.5, slice 8).

- `group_members.drain_deadline`: when a draining member's current run is cancelled;
  `drain_generation`: the generation that started the drain.
- `agent_groups.state`: `active`, `deleting` (members draining) or `deleted`.
- `collections.deleted_at`: a deleted collection is a tombstone; its records and history
  are kept, never removed.
"""

from alembic import op

revision = "m14_group_drain"
down_revision = "m13_state_machines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE group_members ADD COLUMN drain_deadline TEXT")
    op.execute("ALTER TABLE group_members ADD COLUMN drain_generation INTEGER")
    op.execute(
        "ALTER TABLE agent_groups ADD COLUMN state TEXT NOT NULL DEFAULT 'active' "
        "CHECK(state IN ('active','deleting','deleted'))"
    )
    op.execute("ALTER TABLE collections ADD COLUMN deleted_at TEXT")


def downgrade() -> None:
    raise RuntimeError("Group draining cannot be downgraded; restore a verified backup")
