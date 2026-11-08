"""Event triggers and their per-event delivery record (ADR-035)."""

from alembic import op

revision = "m6_004"
down_revision = "m6_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The scheduler evaluates time triggers only; event triggers admit on delivery.
    op.execute(
        "ALTER TABLE triggers ADD COLUMN kind TEXT NOT NULL DEFAULT 'time' "
        "CHECK(kind IN ('time','event'))"
    )
    # One row per decided delivery of a CloudEvent (source, id) to one trigger. An
    # admission commits with its task; a refusal is appended and leaves the event open.
    op.execute("""CREATE TABLE trigger_deliveries (
      delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
      trigger_id TEXT NOT NULL REFERENCES triggers(trigger_id),
      source TEXT NOT NULL,
      event_id TEXT NOT NULL,
      event_type TEXT NOT NULL,
      outcome TEXT NOT NULL CHECK(outcome IN ('admitted','refused')),
      task_id TEXT REFERENCES tasks(task_id),
      code TEXT,
      at TEXT NOT NULL,
      CHECK((outcome = 'admitted') = (task_id IS NOT NULL)),
      CHECK((outcome = 'refused') = (code IS NOT NULL)))""")
    # One event starts at most one task per trigger.
    op.execute(
        "CREATE UNIQUE INDEX trigger_delivery_once ON trigger_deliveries"
        "(trigger_id, source, event_id) WHERE outcome='admitted'"
    )
    op.execute(
        "CREATE INDEX trigger_deliveries_by_trigger ON trigger_deliveries(trigger_id, delivery_id)"
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER trigger_deliveries_no_{action.lower()}
          BEFORE {action} ON trigger_deliveries
          BEGIN SELECT RAISE(ABORT, 'trigger deliveries are append-only'); END""")


def downgrade() -> None:
    raise RuntimeError("Event triggers cannot be downgraded; restore a verified backup")
