"""add smart_diagnosis column to analysis_results

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-28

Adds the JSONB smart_diagnosis column that SmartDiagnosisService writes to
after confirmation and that sync_service.py selects during mobile pull sync.
Migration 0014 omitted this column; without it the Supabase REST query in
_RESULT_COLS fails entirely, blocking all analysis_results sync.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'analysis_results'
                  AND column_name = 'smart_diagnosis'
            ) THEN
                ALTER TABLE analysis_results
                    ADD COLUMN smart_diagnosis JSONB NULL;
            END IF;
        END$$;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE analysis_results DROP COLUMN IF EXISTS smart_diagnosis;
    """)
