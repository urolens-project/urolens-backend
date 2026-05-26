"""smart_diagnosis_outputs — create table and align condition columns to Gout/UTI/Trichomoniasis

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-26

Source: Migration 0015 — STORY-WEB-09 / TASK-WEB-09-5
Depends on: analysis_results
Notes: Renames gn_score → uti_score and nephro_score → tricho_score to match
       the confirmed condition set (Gout, Urinary Tract Infection, Trichomoniasis).
       Creates the table first if it does not yet exist (Supabase may already have it).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    # Create probability_level ENUM if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'probability_level') THEN
                CREATE TYPE probability_level AS ENUM ('LOW', 'MODERATE', 'HIGH');
            END IF;
        END$$;
    """)

    # Create smart_diagnosis_outputs table if it doesn't exist
    op.execute("""
        CREATE TABLE IF NOT EXISTS smart_diagnosis_outputs (
            output_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            result_id   UUID NOT NULL UNIQUE
                            REFERENCES analysis_results(result_id),
            gout_score  probability_level NOT NULL,
            uti_score   probability_level NOT NULL,
            tricho_score probability_level NOT NULL,
            evidence_map JSONB NOT NULL DEFAULT '{}'::jsonb,
            no_significant_indicators BOOLEAN NOT NULL DEFAULT FALSE,
            engine_version VARCHAR(30) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'ATTACHED'
                CHECK (status IN ('ATTACHED', 'FLAGGED_UNAVAILABLE')),
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # Rename legacy columns if they still exist under old names
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'gn_score'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs RENAME COLUMN gn_score TO uti_score;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'nephro_score'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs RENAME COLUMN nephro_score TO tricho_score;
            END IF;
        END$$;
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_result
            ON smart_diagnosis_outputs(result_id);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_scores
            ON smart_diagnosis_outputs(gout_score, uti_score, tricho_score);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_evidence
            ON smart_diagnosis_outputs USING gin(evidence_map);
    """)


def downgrade():
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'uti_score'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs RENAME COLUMN uti_score TO gn_score;
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'tricho_score'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs RENAME COLUMN tricho_score TO nephro_score;
            END IF;
        END$$;
    """)
