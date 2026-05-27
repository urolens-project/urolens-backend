"""create smart_diagnosis_outputs table

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-26

Source: Migration 0015 — STORY-WEB-09 / TASK-WEB-09-5
Depends on: analysis_results
Notes: Creates the smart_diagnosis_outputs table with the three condition
       columns aligned to the SDD: gout_score, gn_score (Glomerulonephritis),
       nephro_score (Nephrolithiasis).
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'probability_level') THEN
                CREATE TYPE probability_level AS ENUM ('LOW', 'MODERATE', 'HIGH');
            END IF;
        END$$;
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS smart_diagnosis_outputs (
            output_id           UUID            NOT NULL DEFAULT gen_random_uuid(),
            result_id           UUID            NOT NULL,
            gout_score          probability_level NOT NULL,
            gn_score            probability_level NOT NULL,
            nephro_score        probability_level NOT NULL,
            evidence_map        JSONB           NOT NULL DEFAULT '{}'::jsonb,
            no_significant_indicators BOOLEAN   NOT NULL DEFAULT FALSE,
            engine_version      VARCHAR(30)     NOT NULL,
            status              VARCHAR(30)     NOT NULL DEFAULT 'ATTACHED'
                CHECK (status IN ('ATTACHED', 'FLAGGED_UNAVAILABLE')),
            generated_at        TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT pk_smart_diagnosis_outputs PRIMARY KEY (output_id),
            CONSTRAINT uq_smart_diagnosis_result  UNIQUE (result_id),
            CONSTRAINT fk_smart_diagnosis_result
                FOREIGN KEY (result_id) REFERENCES analysis_results(result_id)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_result
            ON smart_diagnosis_outputs(result_id);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_scores
            ON smart_diagnosis_outputs(gout_score, gn_score, nephro_score);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_smart_diagnosis_evidence
            ON smart_diagnosis_outputs USING gin(evidence_map);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS smart_diagnosis_outputs;")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'probability_level') THEN
                DROP TYPE probability_level;
            END IF;
        END$$;
    """)
