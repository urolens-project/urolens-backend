"""analysis_results.ai_detections: create the column the model already maps

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-27 00:00:00.000000

`AnalysisResult.aiDetections` (`ai_detections JSONB NULL`, per-particle AI boxes
for the result review screens) was mapped without a migration. Live has the
column: it was added by the `0046` of the closed `feat/review-image-annotation`
branch (#88), which live is stamped with. That revision never merged, and the
`0046` in this chain (`add_missing_live_fks`) does not create it, so a database
built from migrations (the B1 fresh-database path) had no such column and every
query that loads an `AnalysisResult` failed there.

`ADD COLUMN IF NOT EXISTS` makes this a no-op on live (checked 2026-10-27:
`jsonb`, nullable, no default, one row with data) and creates the column
everywhere else. Live's `alembic_version` (`0046`) needs no stamp: this chain's
`0046` only adds two foreign keys live already has.

No new table, so no row-level security change.

Downgrade drops the column only when no row has data in it. On live the column
predates this revision and holds AI output, which a downgrade must not destroy.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: str | Sequence[str] | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `analysis_results.ai_detections` where it is missing."""
    op.execute("ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS ai_detections JSONB;")


def downgrade() -> None:
    """Drop `ai_detections`, refusing while any result still has boxes in it."""
    op.execute(
        """
        DO $$
        BEGIN
            -- Nested, not AND-ed: the inner query names the column, so it may only
            -- be planned once the column is known to exist.
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'analysis_results' AND column_name = 'ai_detections'
            ) THEN
                IF EXISTS (SELECT 1 FROM analysis_results WHERE ai_detections IS NOT NULL) THEN
                    RAISE EXCEPTION '0048 downgrade refused: analysis_results.ai_detections holds data';
                END IF;
            END IF;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE analysis_results DROP COLUMN IF EXISTS ai_detections;")
