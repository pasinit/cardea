"""Collections, records and their append-only history (ADR-034 slice 1).

Claims (slice 2) and state machines (slice 3) reuse the `state`, `holder`, `claim_epoch`,
`lease_expires_at` and `followups` columns and the reserved event kinds, so they need no
further migration of these tables. Groups and mailboxes add their own tables.
"""

from alembic import op

revision = "m8_records"
down_revision = "m8_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One supervisor-managed Collection document per (namespace, name).
    op.execute("""CREATE TABLE collections (
      namespace TEXT NOT NULL,
      name TEXT NOT NULL,
      supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
      generation INTEGER NOT NULL CHECK(generation >= 1),
      spec_json TEXT NOT NULL,
      spec_digest TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(namespace, name))""")
    op.execute("""CREATE TABLE records (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      collection TEXT NOT NULL,
      record_id TEXT NOT NULL,
      data_json TEXT NOT NULL,
      labels_json TEXT NOT NULL DEFAULT '[]',
      links_json TEXT NOT NULL DEFAULT '[]',
      state TEXT,
      holder TEXT,
      claim_epoch INTEGER NOT NULL DEFAULT 0 CHECK(claim_epoch >= 0),
      lease_expires_at TEXT,
      followups INTEGER NOT NULL DEFAULT 0 CHECK(followups >= 0),
      version INTEGER NOT NULL CHECK(version >= 1),
      created_by TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(namespace, collection, record_id),
      FOREIGN KEY(namespace, collection) REFERENCES collections(namespace, name),
      CHECK((holder IS NULL) = (lease_expires_at IS NULL)))""")
    op.execute("CREATE INDEX records_by_state ON records(namespace, collection, state, seq)")
    op.execute("CREATE INDEX records_by_holder ON records(namespace, collection, holder, seq)")
    # The record's history: every change, with its actor, task and receipt.
    op.execute("""CREATE TABLE record_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      collection TEXT NOT NULL,
      record_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('created','updated','claimed','released',
        'claim_expired','transitioned','migrated')),
      actor TEXT NOT NULL,
      task_id TEXT,
      receipt_id TEXT,
      old_json TEXT,
      new_json TEXT NOT NULL,
      at TEXT NOT NULL,
      FOREIGN KEY(namespace, collection, record_id)
        REFERENCES records(namespace, collection, record_id))""")
    op.execute(
        "CREATE INDEX record_events_by_record ON record_events"
        "(namespace, collection, record_id, event_id)"
    )
    # A receipt changes at most one record once; reconciliation looks the change up by it.
    op.execute(
        "CREATE UNIQUE INDEX record_events_by_receipt ON record_events(receipt_id) "
        "WHERE receipt_id IS NOT NULL"
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER record_events_no_{action.lower()}
          BEFORE {action} ON record_events
          BEGIN SELECT RAISE(ABORT, 'record history is append-only'); END""")
    # Records are never removed; a later slice may add an explicit, recorded archive.
    op.execute("""CREATE TRIGGER records_no_delete BEFORE DELETE ON records
          BEGIN SELECT RAISE(ABORT, 'records are not deleted'); END""")


def downgrade() -> None:
    raise RuntimeError("Records cannot be downgraded; restore a verified backup")
