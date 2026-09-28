"""supervisor dashboard stats: supporting indexes

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-28 00:00:00.000000

Renumbered from 0040 to 0042: this branch (UROLENS-169) originally chained
this migration after the then-tip (0039). By the time it merged,
origin/development had already landed UROLENS-220's own 0040
(0040_enable_rls_all_tables.py) and 0041 (0041_make_microscopy_bucket_
private.py), so this file's `revision = '0040'` collided with the RLS
migration's — Alembic reported two heads and, worse, `test_rls_migration.py`
started resolving revision '0040' to *this* file instead of the RLS one
(ambiguous module lookup by revision id). Chained after the real tip (0041)
instead. Content unchanged from the original 0040 version.

UROLENS-169: `GET /api/v1/results/supervisor/stats` (and the three queue-list
endpoints it summarizes — `/pending`, `/approved-today`, `/escalated`) filter
on `analysis_results.status`, `specimens.status`, and range-filter on
`result_approvals.approved_at`. None of the three columns had an index
before this migration — `analysis_results.status` and `specimens.status`
are plain unindexed columns, and `result_approvals` only indexes
`result_id` (migration 0019). This endpoint is polled on an interval by the
supervisor dashboard, so a sequential scan on every poll doesn't scale past
a small table. Plain (non-partial) btree indexes: all three columns are
also filtered with other predicates elsewhere in this same service
(e.g. `CRITICAL_ESCALATED` and `PENDING_SUPERVISOR_APPROVAL` are both
looked up against `analysis_results.status`), so a single per-column index
serves every current caller rather than one baked to a single query shape.

NOT RUN AGAINST THE LIVE DATABASE — same standing constraint as every
migration in this chain (see 0039's docstring): this environment has no
network access to the real DB.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0042'
down_revision: str | Sequence[str] | None = '0041'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_analysis_results_status "
        "ON analysis_results (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_specimens_status "
        "ON specimens (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_result_approvals_approved_at "
        "ON result_approvals (approved_at)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_result_approvals_approved_at")
    op.execute("DROP INDEX IF EXISTS ix_specimens_status")
    op.execute("DROP INDEX IF EXISTS ix_analysis_results_status")
