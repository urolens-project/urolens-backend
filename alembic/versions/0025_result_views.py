"""create result_views table

Revision ID: 0025
Revises: 0016
Create Date: 2026-05-26

Source: Migration 0025 — STORY-WEB-16 audit trail for patient result viewing
"""

from alembic import op

revision = "0025"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS result_views (
            view_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id  UUID        NOT NULL REFERENCES analysis_results(result_id) ON DELETE CASCADE,
            patient_id UUID        NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
            viewed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("ALTER TABLE result_views DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE result_views ENABLE ROW LEVEL SECURITY;")
    op.drop_table("result_views")
