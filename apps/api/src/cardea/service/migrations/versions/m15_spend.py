"""Spend accounts, per-attempt run cost and windowed expectation reports (ADR-034 slice 9)."""

from alembic import op

revision = "m15_spend"
down_revision = "m14_group_drain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # What each agent-run attempt reported spending (ADR-033 metering); NULL is unknown.
    # Written in the attempt's own finishing transaction, for every agent run.
    op.execute("""CREATE TABLE run_attempt_costs (
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      attempt INTEGER NOT NULL CHECK(attempt >= 1),
      cost_usd REAL CHECK(cost_usd IS NULL OR cost_usd >= 0),
      recorded_at TEXT NOT NULL,
      PRIMARY KEY(task_id, attempt))""")
    # One reservation per group-member run, charged to the group's and the pool's spend
    # window at admission (ADR-015 style). It settles to the run's reported cost once the
    # run is terminal, or stays `unknown` (the full reservation counts) when any attempt's
    # cost is unknown.
    op.execute("""CREATE TABLE spend_reservations (
      task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
      namespace TEXT NOT NULL,
      group_name TEXT NOT NULL,
      pool TEXT NOT NULL,
      group_window TEXT,
      pool_window TEXT,
      reserved_usd REAL NOT NULL CHECK(reserved_usd > 0),
      state TEXT NOT NULL CHECK(state IN ('reserved','settled','unknown')),
      cost_usd REAL CHECK(cost_usd IS NULL OR cost_usd >= 0),
      created_at TEXT NOT NULL,
      settled_at TEXT,
      CHECK((state = 'settled') = (cost_usd IS NOT NULL)))""")
    op.execute(
        "CREATE INDEX spend_by_group_window "
        "ON spend_reservations(namespace, group_name, group_window)"
    )
    op.execute(
        "CREATE INDEX spend_by_pool_window "
        "ON spend_reservations(namespace, group_name, pool, pool_window)"
    )
    # One report per windowed expectation and closed window, so window-close mail is sent
    # once. `entry_sha256` identifies the expectation across manifest edits.
    op.execute("""CREATE TABLE expect_window_reports (
      namespace TEXT NOT NULL,
      group_name TEXT NOT NULL,
      pool TEXT NOT NULL,
      entry_sha256 TEXT NOT NULL,
      window_key TEXT NOT NULL,
      outcome TEXT NOT NULL CHECK(outcome IN ('met','expectation_unmet','expectation_exceeded')),
      observed INTEGER NOT NULL CHECK(observed >= 0),
      message_id TEXT,
      reported_at TEXT NOT NULL,
      PRIMARY KEY(namespace, group_name, pool, entry_sha256, window_key))""")
    for table in ("run_attempt_costs", "expect_window_reports"):
        op.execute(f"""CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
          BEGIN SELECT RAISE(ABORT, '{table} rows are append-only'); END""")


def downgrade() -> None:
    raise RuntimeError("Spend accounts cannot be downgraded; restore a verified backup")
