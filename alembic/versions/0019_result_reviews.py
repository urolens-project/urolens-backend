"""create result_reviews, result_approvals, result_returns, escalations tables

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-26

Source: Migration 0019 — T3.2 Result Review
Depends on: analysis_results, users
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'escalation_path') THEN
                CREATE TYPE escalation_path AS ENUM (
                    'NOTIFY_PHYSICIAN',
                    'FLAG_SENIOR_REVIEW',
                    'MARK_CRITICAL'
                );
            END IF;
        END$$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS result_reviews (
            review_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id   UUID        NOT NULL REFERENCES analysis_results(result_id) ON DELETE RESTRICT,
            reviewed_by UUID        NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            annotation_notes TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_result_reviews_result ON result_reviews(result_id);")

    op.execute("""
        CREATE TABLE IF NOT EXISTS result_approvals (
            approval_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id   UUID        NOT NULL REFERENCES analysis_results(result_id) ON DELETE RESTRICT,
            approved_by UUID        NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            notes       TEXT,
            approved_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_result_approvals_result ON result_approvals(result_id);")

    op.execute("""
        CREATE TABLE IF NOT EXISTS result_returns (
            return_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id   UUID        NOT NULL REFERENCES analysis_results(result_id) ON DELETE RESTRICT,
            returned_by UUID        NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            reason      TEXT        NOT NULL,
            returned_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_result_returns_result ON result_returns(result_id);")

    op.execute("""
        CREATE TABLE IF NOT EXISTS escalations (
            escalation_id   UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id       UUID            NOT NULL REFERENCES analysis_results(result_id) ON DELETE RESTRICT,
            escalated_by    UUID            NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            escalation_path escalation_path NOT NULL,
            escalation_note TEXT,
            escalated_at    TIMESTAMPTZ     NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_escalations_result ON escalations(result_id);")


def downgrade():
    op.execute("DROP TABLE IF EXISTS escalations;")
    op.execute("DROP TABLE IF EXISTS result_returns;")
    op.execute("DROP TABLE IF EXISTS result_approvals;")
    op.execute("DROP TABLE IF EXISTS result_reviews;")
    op.execute("DROP TYPE IF EXISTS escalation_path;")
