"""create result_releases table

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-26

Source: Migration 0026 — STORY-WEB-15 Result Releasing (T4.1)
Depends on: analysis_results, users
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'release_method') THEN
                CREATE TYPE release_method AS ENUM ('PHYSICAL', 'DIGITAL');
            END IF;
        END$$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS result_releases (
            release_id    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id     UUID          NOT NULL REFERENCES analysis_results(result_id) ON DELETE RESTRICT,
            released_by   UUID          NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            release_method release_method NOT NULL,
            released_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_result_releases_result ON result_releases(result_id);")
    op.execute("ALTER TABLE result_releases DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE result_releases ENABLE ROW LEVEL SECURITY;")
    op.execute("DROP TABLE IF EXISTS result_releases;")
    op.execute("DROP TYPE IF EXISTS release_method;")
