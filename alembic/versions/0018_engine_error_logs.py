"""create engine_error_logs table

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-26

Source: Migration 0018 — T3.1 Alt Flow 2 Engine failure handling
Depends on: analysis_results
Notes: Records Smart Diagnosis engine failures. Does not block MedTech
       confirmation — the failure is isolated from the confirm flow.
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS engine_error_logs (
            error_id        UUID        NOT NULL DEFAULT gen_random_uuid(),
            result_id       UUID        NOT NULL,
            error_code      VARCHAR(80) NOT NULL,
            error_message   TEXT        NOT NULL,
            stack_trace     TEXT,
            flagged_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT pk_engine_error_logs  PRIMARY KEY (error_id),
            CONSTRAINT fk_engine_errors_result
                FOREIGN KEY (result_id) REFERENCES analysis_results(result_id)
        );
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_engine_errors_result
            ON engine_error_logs(result_id);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS engine_error_logs;")
