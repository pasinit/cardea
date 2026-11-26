"""The W3C traceparent of an agent run's root span (tracing 1/3).

One trace per run: the run's traceparent is stored with its first attempt and every attempt
(retry, resume, restart) is a child span of it. Existing runs have none and get one at their
next attempt.
"""

from alembic import op

revision = "m20_run_traceparent"
down_revision = "m19_claim_attempt_renewal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_runs ADD COLUMN traceparent TEXT")


def downgrade() -> None:
    raise RuntimeError("Run trace state cannot be downgraded; restore a verified backup")
