"""Record state machines (ADR-034 slice 3) and resource-qualified approval (ADR-036 amendment).

- `records.claim_transition`: the transition that acquired the current claim, so an expired
  claim returns the record to that transition's `on_claim_expiry` state.
- `record_events.detail_json`: what a change was about beyond old and new values, such as
  the transition, its arguments and the cited receipt.
- `approval_requests.approval_resource`: the right whose `approve` mode decides a request,
  e.g. `record:orders/approve`; null means the operation name, as before.
"""

from alembic import op

revision = "m13_state_machines"
down_revision = "m12_journal_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE records ADD COLUMN claim_transition TEXT")
    op.execute("ALTER TABLE record_events ADD COLUMN detail_json TEXT")
    op.execute("ALTER TABLE approval_requests ADD COLUMN approval_resource TEXT")


def downgrade() -> None:
    raise RuntimeError("State machines cannot be downgraded; restore a verified backup")
