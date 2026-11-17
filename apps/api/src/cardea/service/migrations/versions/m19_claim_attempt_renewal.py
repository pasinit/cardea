"""Claims renewed by an attempt start only after progress (ADR-034 amendment, 2026-11-17).

`records.attempt_renewal` is 1 while the holder's last attempt on the claim did not fail:
the next attempt start may then extend the claim past that attempt. An attempt start sets
it to 0 until the attempt proves progress (a successful run, a successful session that
parks for approval, or a successful claim call); a failed or crashed attempt leaves it 0,
so the attempts after it do not extend the claim, which lapses at its current expiry.
Existing claims start at 1: the behaviour before this revision.
"""

from alembic import op

revision = "m19_claim_attempt_renewal"
down_revision = "m18_env_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE records ADD COLUMN attempt_renewal INTEGER NOT NULL DEFAULT 1 "
        "CHECK(attempt_renewal IN (0,1))"
    )


def downgrade() -> None:
    raise RuntimeError("Claim renewal state cannot be downgraded; restore a verified backup")
