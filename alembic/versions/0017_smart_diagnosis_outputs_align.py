"""align smart_diagnosis_outputs columns to SDD schema

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-26

Source: Migration 0017 — T3.1 Smart Diagnosis Engine (SDD alignment)
Depends on: analysis_results (0014), smart_diagnosis_outputs (0015)
Notes: Ensures the table has the exact SDD-specified columns.
       Idempotent — renames any legacy column aliases if present,
       and adds missing columns (no_significant_indicators, engine_version, status).
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    # Rename uti_score → gn_score if it still exists from an older migration run
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
        END$$;
    """)

    # Rename tricho_score → nephro_score if it still exists
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'tricho_score'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs RENAME COLUMN tricho_score TO nephro_score;
            END IF;
        END$$;
    """)

    # Add no_significant_indicators if missing
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'no_significant_indicators'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs
                    ADD COLUMN no_significant_indicators BOOLEAN NOT NULL DEFAULT FALSE;
            END IF;
        END$$;
    """)

    # Add engine_version if missing
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'engine_version'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs
                    ADD COLUMN engine_version VARCHAR(30) NOT NULL DEFAULT 'mvp-v1.0';
            END IF;
        END$$;
    """)

    # Add status if missing
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'smart_diagnosis_outputs'
                  AND column_name = 'status'
            ) THEN
                ALTER TABLE smart_diagnosis_outputs
                    ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'ATTACHED';
            END IF;
        END$$;
    """)

    # Refresh indexes with correct column names
    op.execute("""
        DROP INDEX IF EXISTS idx_smart_diagnosis_scores;
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_scores
            ON smart_diagnosis_outputs(gout_score, gn_score, nephro_score);
    """)


def downgrade():
    # Best-effort reverse — rename back to legacy names
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
