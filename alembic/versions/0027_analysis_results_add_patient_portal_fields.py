"""add patient_portal columns to analysis_results

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-26

Source: Migration 0015 — STORY-WEB-16 Patient Result Viewing
Depends on: analysis_results (0014), patients (0004), users (0001)
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'result_status') THEN
                CREATE TYPE result_status AS ENUM (
                    'PENDING_CONFIRM',
                    'PENDING_SUPERVISOR_APPROVAL',
                    'APPROVED',
                    'RELEASED',
                    'RETURNED_FOR_CORRECTION',
                    'CRITICAL_ESCALATED'
                );
            END IF;
        END$$;
    """)

    op.execute("ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'PENDING';")
    op.execute("ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'CONFIRMED';")

    op.execute("ALTER TABLE analysis_results DISABLE ROW LEVEL SECURITY;")

    op.add_column("analysis_results",
        sa.Column("patient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("patients.patient_id", ondelete="RESTRICT"),
                  nullable=True))
    op.add_column("analysis_results",
        sa.Column("cell_counts", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True))
    op.add_column("analysis_results",
        sa.Column("interpretation", sa.Text(), nullable=True))
    op.add_column("analysis_results",
        sa.Column("medtech_name", sa.VARCHAR(255), nullable=True))
    op.add_column("analysis_results",
        sa.Column("pathologist_name", sa.VARCHAR(255), nullable=True))
    op.add_column("analysis_results",
        sa.Column("pathologist_license", sa.VARCHAR(100), nullable=True))
    op.add_column("analysis_results",
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'analysis_results' AND column_name = 'medtech_id'
            ) THEN
                ALTER TABLE analysis_results
                ADD COLUMN medtech_id UUID REFERENCES users(user_id) ON DELETE RESTRICT;
            END IF;
        END$$;
    """)


def downgrade():
    op.execute("ALTER TABLE analysis_results ENABLE ROW LEVEL SECURITY;")
    op.drop_column("analysis_results", "released_at")
    op.drop_column("analysis_results", "pathologist_license")
    op.drop_column("analysis_results", "pathologist_name")
    op.drop_column("analysis_results", "medtech_name")
    op.drop_column("analysis_results", "medtech_id")
    op.drop_column("analysis_results", "interpretation")
    op.drop_column("analysis_results", "cell_counts")
    op.drop_column("analysis_results", "patient_id")
