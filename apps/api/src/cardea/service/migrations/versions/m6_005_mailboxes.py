"""Mailboxes: durable messages between principals and their deliveries (ADR-034 slice 5)."""

from alembic import op

revision = "m6_005"
down_revision = "m6_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per message. It commits in its sender's receipt transaction (an agent's
    # `mailbox.send`), in the supervisor's API request, or with the delivery whose dead
    # letter it reports (`kind='notice'`, sent by the kernel's runtime principal).
    op.execute("""CREATE TABLE mailbox_messages (
      message_id TEXT PRIMARY KEY,
      namespace TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('message','notice')),
      sender_id TEXT NOT NULL REFERENCES principals(principal_id),
      sender_task_id TEXT REFERENCES tasks(task_id),
      sender_receipt_id TEXT,
      supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
      client_key TEXT,
      request_sha256 TEXT NOT NULL,
      to_kind TEXT NOT NULL CHECK(to_kind IN ('instance','pool','group')),
      to_address TEXT NOT NULL,
      record_json TEXT,
      parts_json TEXT NOT NULL,
      body_bytes INTEGER NOT NULL,
      max_deliveries INTEGER NOT NULL CHECK(max_deliveries BETWEEN 1 AND 10),
      about_message_id TEXT REFERENCES mailbox_messages(message_id),
      created_at TEXT NOT NULL,
      CHECK((kind = 'notice') = (about_message_id IS NOT NULL)),
      CHECK(sender_receipt_id IS NULL OR sender_task_id IS NOT NULL))""")
    # A send is idempotent on its client key per sender (API) and on its receipt (agents).
    op.execute(
        "CREATE UNIQUE INDEX mailbox_client_key ON mailbox_messages(namespace, sender_id, "
        "client_key) WHERE client_key IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX mailbox_sender_receipt ON mailbox_messages(sender_receipt_id) "
        "WHERE sender_receipt_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX mailbox_messages_by_namespace "
        "ON mailbox_messages(namespace, created_at, message_id)"
    )
    # One row per recipient. An instance or group message names its principal; a pool
    # message names the pool and is bound to the first member that leases it.
    op.execute("""CREATE TABLE mailbox_deliveries (
      delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
      message_id TEXT NOT NULL REFERENCES mailbox_messages(message_id),
      namespace TEXT NOT NULL,
      recipient_id TEXT REFERENCES principals(principal_id),
      pool TEXT,
      state TEXT NOT NULL CHECK(state IN ('pending','leased','consumed','dead')),
      task_id TEXT REFERENCES tasks(task_id),
      attempt INTEGER,
      deliveries INTEGER NOT NULL DEFAULT 0 CHECK(deliveries >= 0),
      updated_at TEXT NOT NULL,
      CHECK(recipient_id IS NOT NULL OR pool IS NOT NULL),
      CHECK((state IN ('leased','consumed')) = (task_id IS NOT NULL)),
      CHECK((state = 'leased') = (attempt IS NOT NULL)))""")
    op.execute(
        "CREATE INDEX mailbox_inbox ON mailbox_deliveries(namespace, recipient_id, state, "
        "delivery_id)"
    )
    op.execute(
        "CREATE INDEX mailbox_pool_inbox ON mailbox_deliveries(namespace, pool, state, "
        "delivery_id) WHERE pool IS NOT NULL"
    )
    op.execute("CREATE INDEX mailbox_by_task ON mailbox_deliveries(task_id, state)")
    op.execute("CREATE INDEX mailbox_leased ON mailbox_deliveries(task_id) WHERE state='leased'")
    op.execute("CREATE INDEX mailbox_by_message ON mailbox_deliveries(message_id, delivery_id)")
    # Every change of a delivery's state, in order: its delivery history.
    op.execute("""CREATE TABLE mailbox_delivery_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      delivery_id INTEGER NOT NULL REFERENCES mailbox_deliveries(delivery_id),
      kind TEXT NOT NULL CHECK(kind IN ('created','leased','consumed','released','dead')),
      task_id TEXT REFERENCES tasks(task_id),
      attempt INTEGER,
      at TEXT NOT NULL)""")
    op.execute(
        "CREATE INDEX mailbox_delivery_history ON mailbox_delivery_events(delivery_id, event_id)"
    )
    for table in ("mailbox_messages", "mailbox_delivery_events"):
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""CREATE TRIGGER {table}_no_{action.lower()}
              BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""")
    op.execute("""CREATE TRIGGER mailbox_deliveries_no_delete
      BEFORE DELETE ON mailbox_deliveries
      BEGIN SELECT RAISE(ABORT, 'mailbox deliveries are never deleted'); END""")
    # Settled deliveries are final; only pending and leased rows change.
    op.execute("""CREATE TRIGGER mailbox_deliveries_settled
      BEFORE UPDATE ON mailbox_deliveries WHEN OLD.state IN ('consumed','dead')
      BEGIN SELECT RAISE(ABORT, 'settled mailbox deliveries are final'); END""")


def downgrade() -> None:
    raise RuntimeError("Mailboxes cannot be downgraded; restore a verified backup")
