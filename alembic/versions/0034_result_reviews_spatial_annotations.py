"""result reviews spatial annotations

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-21 21:45:03.209825

Fixes a real regression from the results-domain SQLAlchemy port (step 5b):
`result_reviews.spatial_annotations` had no Alembic history anywhere in
this repo, so it was never mapped in the new ResultReview model, and
`annotate_result` silently dropped every caller-supplied value instead of
persisting it — no error, no signal. Supervisor annotations made since that
port would have been lost.

TYPE INFERRED, NOT VERIFIED AGAINST A LIVE DATABASE. This environment has
no network access to confirm the live schema. JSONB is inferred from:
  - The pre-port code (recovered via `git show 35ab942^:app/schemas/results.py`
    and `git show 35ab942^:app/services/result_review_service.py`, both
    deleted/pruned in that commit): the field was always typed
    `Optional[List[Dict[str, Any]]]` and passed straight through as a raw
    Python list to the Supabase-REST insert/update payload — the same
    pattern this codebase uses everywhere else for JSONB columns
    (AnalysisResult.ai_findings/.flagged_anomalies/.particle_classes,
    SmartDiagnosisOutput.evidence_map).
  - No evidence contradicts JSONB, but none of it is a direct schema read
    either. Needs the same verification pass as migrations 0032/0033 before
    this is safe to run against any environment where the column doesn't
    already exist in exactly this shape (type, nullability). If the live
    column turns out to be a different type (e.g. plain JSON, or a
    Postgres array type), this migration and the ResultReview model's
    `spatial_annotations` mapping both need to change together.

Uses the same idempotent-guard idiom as 0032/0033 (ADD COLUMN IF NOT
EXISTS) rather than assuming the column is absent — the whole premise of
this finding is that it may already exist live in some shape.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0034'
down_revision: Union[str, Sequence[str], None] = '0033'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE result_reviews ADD COLUMN IF NOT EXISTS spatial_annotations JSONB"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE result_reviews DROP COLUMN IF EXISTS spatial_annotations")
