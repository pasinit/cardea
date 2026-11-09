"""Kernel journal events as an event-trigger source (ADR-034 slice 7, ADR-035)."""

from alembic import op

revision = "m12_journal_triggers"
down_revision = "m11_mailboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Triggers gain `kind = 'journal'`. The CHECK is on the column, so swap the column.
    op.execute(
        "ALTER TABLE triggers ADD COLUMN kind_v2 TEXT NOT NULL DEFAULT 'time' "
        "CHECK(kind_v2 IN ('time','event','journal'))"
    )
    op.execute("UPDATE triggers SET kind_v2=kind")
    op.execute("ALTER TABLE triggers DROP COLUMN kind")
    op.execute("ALTER TABLE triggers RENAME COLUMN kind_v2 TO kind")
    # How far each journal trigger has read its namespace's journal. Installation starts
    # it at the journal's end, so a new trigger never replays history. `pending_cursor` is
    # the newest event coalesced while the member's run had already started: the run may
    # not have seen it, so one follow-up run is admitted for it once the member is free.
    op.execute("""CREATE TABLE journal_trigger_cursors (
      trigger_id TEXT PRIMARY KEY REFERENCES triggers(trigger_id),
      cursor INTEGER NOT NULL CHECK(cursor >= 0),
      pending_cursor INTEGER CHECK(pending_cursor IS NULL OR pending_cursor <= cursor),
      updated_at TEXT NOT NULL)""")
    op.execute("""CREATE TRIGGER journal_trigger_cursors_no_delete
      BEFORE DELETE ON journal_trigger_cursors
      BEGIN SELECT RAISE(ABORT, 'journal trigger cursors are never deleted'); END""")
    op.execute("""CREATE TRIGGER journal_trigger_cursors_forward
      BEFORE UPDATE OF cursor ON journal_trigger_cursors WHEN NEW.cursor < OLD.cursor
      BEGIN SELECT RAISE(ABORT, 'a journal cursor only moves forward'); END""")


def downgrade() -> None:
    raise RuntimeError("Journal triggers cannot be downgraded; restore a verified backup")
