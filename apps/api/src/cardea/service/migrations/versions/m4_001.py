"""Immutable delegation slots; execution state stays in the existing tasks table."""

from alembic import op

revision = "m4_001"
down_revision = "m3_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE delegation_links (
        parent_id TEXT NOT NULL REFERENCES tasks(task_id),
        slot INTEGER NOT NULL CHECK(slot BETWEEN 0 AND 3),
        child_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
        message_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY(parent_id, slot)
    )""")

    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER delegation_no_{action.lower()} BEFORE {action} "
            "ON delegation_links BEGIN SELECT RAISE(ABORT,'delegation is immutable'); END"
        )


def downgrade() -> None:
    op.execute("DROP TABLE delegation_links")
