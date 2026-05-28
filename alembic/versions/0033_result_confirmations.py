"""create result_confirmations table

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-28

ResultConfirmationService (used by the confirm endpoint since Epic 7) inserts a
ResultConfirmation row to audit each MedTech confirmation and enforce a
DB-level uniqueness guard against concurrent double-submits.
Without this table the confirmation endpoint raises ProgrammingError on every
call, silently aborting confirmation and preventing SmartDiagnosis from running.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    # Add confirmation_notes to analysis_results (used by get_full_result and
    # the old Supabase-based confirm path)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'analysis_results'
                  AND column_name = 'confirmation_notes'
            ) THEN
                ALTER TABLE analysis_results
                    ADD COLUMN confirmation_notes TEXT NULL;
            END IF;
        END$$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS result_confirmations (
            confirmation_id  UUID        NOT NULL DEFAULT gen_random_uuid(),
            result_id        UUID        NOT NULL,
            medtech_id       UUID        NOT NULL,
            confirmed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT pk_result_confirmations PRIMARY KEY (confirmation_id),
            CONSTRAINT uq_result_confirmations_result UNIQUE (result_id),
            CONSTRAINT fk_result_confirmations_result
                FOREIGN KEY (result_id) REFERENCES analysis_results(result_id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_result_confirmations_medtech
                FOREIGN KEY (medtech_id) REFERENCES users(user_id)
                ON DELETE RESTRICT
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_result_confirmations_result
            ON result_confirmations(result_id);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS result_confirmations;")
    op.execute(
        "ALTER TABLE analysis_results DROP COLUMN IF EXISTS confirmation_notes;"
    )
