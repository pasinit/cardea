"""Time triggers and their append-only slot history (ADR-024)."""

from alembic import op

revision = "m6_002"
down_revision = "m6_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per installed trigger; removal is a flag so history keeps its parent.
    # `anchor` is the instant after which slots count (install or re-enable time);
    # `last_slot` is the newest slot already decided, in the same commit as its outcome.
    op.execute("""CREATE TABLE triggers (
      trigger_id TEXT PRIMARY KEY,
      supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
      namespace TEXT NOT NULL,
      definition_json TEXT NOT NULL,
      definition_sha256 TEXT NOT NULL,
      enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
      removed INTEGER NOT NULL CHECK(removed IN (0,1)),
      anchor TEXT NOT NULL,
      last_slot TEXT,
      updated_at TEXT NOT NULL)""")
    op.execute("""CREATE TRIGGER triggers_no_delete BEFORE DELETE ON triggers
      BEGIN SELECT RAISE(ABORT, 'triggers are removed by flag, never deleted'); END""")
    op.execute("""CREATE TABLE trigger_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      trigger_id TEXT NOT NULL REFERENCES triggers(trigger_id),
      kind TEXT NOT NULL CHECK(kind IN (
        'installed','updated','enabled','disabled','removed','admitted','failed','missed')),
      slot TEXT,
      task_id TEXT REFERENCES tasks(task_id),
      code TEXT,
      detail_json TEXT NOT NULL,
      at TEXT NOT NULL,
      CHECK((kind IN ('admitted','failed')) = (slot IS NOT NULL)),
      CHECK((kind = 'admitted') = (task_id IS NOT NULL)))""")
    # A slot has exactly one outcome: admitted or failed.
    op.execute("CREATE UNIQUE INDEX trigger_slot_once ON trigger_events(trigger_id, slot)")
    op.execute("CREATE INDEX trigger_events_by_trigger ON trigger_events(trigger_id, event_id)")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER trigger_events_no_{action.lower()}
          BEFORE {action} ON trigger_events
          BEGIN SELECT RAISE(ABORT, 'trigger history is append-only'); END""")


def downgrade() -> None:
    raise RuntimeError("Trigger history cannot be downgraded; restore a verified backup")
