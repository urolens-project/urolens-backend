"""create result_views table

Revision ID: 0025
Revises: 0016
Create Date: 2026-05-26

Source: Migration 0025 — STORY-WEB-16 audit trail for patient result viewing
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("result_views",
        sa.Column("view_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("result_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("analysis_results.result_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("patients.patient_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("viewed_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.execute("ALTER TABLE result_views DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE result_views ENABLE ROW LEVEL SECURITY;")
    op.drop_table("result_views")
