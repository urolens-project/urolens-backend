"""Persist individual AI detections for the mobile result image overlay.

Revision ID: 0046
Revises: 0045

Existing counts cannot reconstruct geometry, so legacy rows remain NULL.
New analyses write [] when inference succeeds without any detections.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | Sequence[str] | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable storage without inventing geometry for existing images."""
    op.execute("ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS ai_detections JSONB")


def downgrade() -> None:
    """Remove detection storage."""
    op.execute("ALTER TABLE analysis_results DROP COLUMN IF EXISTS ai_detections")
