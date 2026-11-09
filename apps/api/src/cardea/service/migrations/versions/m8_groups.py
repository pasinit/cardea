"""Agent groups and their member principals (ADR-034 slice 4)."""

from alembic import op

revision = "m8_groups"
down_revision = "m6_004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per applied group. `generation` counts spec changes; `observed_generation`
    # is the newest generation the reconciler converged.
    op.execute("""CREATE TABLE agent_groups (
      namespace TEXT NOT NULL,
      name TEXT NOT NULL,
      supervisor_id TEXT NOT NULL REFERENCES principals(principal_id),
      generation INTEGER NOT NULL CHECK(generation >= 1),
      observed_generation INTEGER NOT NULL CHECK(observed_generation >= 0),
      spec_json TEXT NOT NULL,
      spec_sha256 TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(namespace, name))""")
    # A member is a runtime-bound agent principal `<group>.<pool>.<n>`, created by
    # `groups.manage`. Members are disabled, never deleted (ADR-034 decision 1.2).
    op.execute("""CREATE TABLE group_members (
      principal_id TEXT PRIMARY KEY REFERENCES principals(principal_id),
      namespace TEXT NOT NULL,
      group_name TEXT NOT NULL,
      pool TEXT NOT NULL,
      ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
      state TEXT NOT NULL CHECK(state IN ('active','draining','removed')),
      created_at TEXT NOT NULL,
      UNIQUE(namespace, group_name, pool, ordinal),
      FOREIGN KEY(namespace, group_name) REFERENCES agent_groups(namespace, name))""")
    for table in ("agent_groups", "group_members"):
        op.execute(f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
          BEGIN SELECT RAISE(ABORT, 'groups and members are removed by state, never deleted');
          END""")
    # One run per member at a time looks up a member's unfinished task.
    op.execute("CREATE INDEX task_bindings_by_agent ON task_bindings(agent_id)")

    # Triggers gain `managed_by = 'group'`. The CHECK is on the column, so swap the column.
    op.execute(
        "ALTER TABLE triggers ADD COLUMN managed_by_v2 TEXT NOT NULL DEFAULT 'host' "
        "CHECK(managed_by_v2 IN ('host','supervisor','group'))"
    )
    op.execute("UPDATE triggers SET managed_by_v2=managed_by")
    op.execute("ALTER TABLE triggers DROP COLUMN managed_by")
    op.execute("ALTER TABLE triggers RENAME COLUMN managed_by_v2 TO managed_by")

    # Slot history gains `coalesced`: a member's slot that arrived while its previous run
    # was unfinished, recorded against that run and not admitted (ADR-034 decision 1.4).
    op.execute("""CREATE TABLE trigger_events_v2 (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      trigger_id TEXT NOT NULL REFERENCES triggers(trigger_id),
      kind TEXT NOT NULL CHECK(kind IN (
        'installed','updated','enabled','disabled','removed','admitted','failed','missed',
        'coalesced')),
      slot TEXT,
      task_id TEXT REFERENCES tasks(task_id),
      code TEXT,
      detail_json TEXT NOT NULL,
      at TEXT NOT NULL,
      CHECK((kind IN ('admitted','failed','coalesced')) = (slot IS NOT NULL)),
      CHECK((kind IN ('admitted','coalesced')) = (task_id IS NOT NULL)))""")
    op.execute(
        "INSERT INTO trigger_events_v2(event_id,trigger_id,kind,slot,task_id,code,detail_json,at) "
        "SELECT event_id,trigger_id,kind,slot,task_id,code,detail_json,at FROM trigger_events"
    )
    # Dropping a table fires none of its triggers; the append-only triggers go with it.
    op.execute("DROP TABLE trigger_events")
    op.execute("ALTER TABLE trigger_events_v2 RENAME TO trigger_events")
    op.execute("CREATE UNIQUE INDEX trigger_slot_once ON trigger_events(trigger_id, slot)")
    op.execute("CREATE INDEX trigger_events_by_trigger ON trigger_events(trigger_id, event_id)")
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER trigger_events_no_{action.lower()}
          BEFORE {action} ON trigger_events
          BEGIN SELECT RAISE(ABORT, 'trigger history is append-only'); END""")


def downgrade() -> None:
    raise RuntimeError("Agent groups cannot be downgraded; restore a verified backup")
