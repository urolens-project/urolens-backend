"""create manual_overrides table

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26

Source: Migration 0016 — T2.6 Manual Override
Depends on: analysis_results, users
Notes: Stores MedTech corrections to individual AI-detected parameters.
       Both the original AI value and the corrected value are stored permanently.
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS manual_overrides (
            override_id         UUID            NOT NULL DEFAULT gen_random_uuid(),
            result_id           UUID            NOT NULL,
            medtech_id          UUID            NOT NULL,
            parameter_name      VARCHAR(100)    NOT NULL,
            original_ai_value   INTEGER         NOT NULL,
            corrected_value     INTEGER         NOT NULL,
            rationale           TEXT,
            overridden_at       TIMESTAMPTZ     NOT NULL DEFAULT now(),

            CONSTRAINT pk_manual_overrides      PRIMARY KEY (override_id),
            CONSTRAINT fk_overrides_result
                FOREIGN KEY (result_id) REFERENCES analysis_results(result_id),
            CONSTRAINT fk_overrides_medtech
                FOREIGN KEY (medtech_id) REFERENCES users(user_id)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_manual_overrides_result
            ON manual_overrides(result_id);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS manual_overrides;")
