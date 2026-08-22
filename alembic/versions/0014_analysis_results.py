"""create analysis_results table

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-23

Source: Migration 0014 — T2.5, T2.6, T3.1, T3.2, T3.3, T3.4, T3.5
Depends on: specimens, images, users
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    # Create result_status ENUM if it doesn't exist
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

    op.create_table(
        "analysis_results",
        sa.Column(
            "result_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "specimen_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "image_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("images.image_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "ai_findings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "flagged_anomalies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "particle_classes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "model_version",
            sa.String(30),
            nullable=False,
            server_default="mvp-v1.0",
        ),
        sa.Column(
            "status",
            sa.String(48),
            nullable=False,
            server_default="PENDING_CONFIRM",
        ),
        sa.Column(
            "smart_diagnosis_unavailable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "confirmed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index("idx_analysis_results_status", "analysis_results", ["status"])
    op.create_index("idx_analysis_results_specimen", "analysis_results", ["specimen_id"])
    op.create_index(
        "idx_analysis_results_ai_findings",
        "analysis_results",
        ["ai_findings"],
        postgresql_using="gin",
    )


def downgrade():
    op.drop_index("idx_analysis_results_ai_findings", table_name="analysis_results")
    op.drop_index("idx_analysis_results_specimen", table_name="analysis_results")
    op.drop_index("idx_analysis_results_status", table_name="analysis_results")
    op.drop_table("analysis_results")
    op.execute("DROP TYPE IF EXISTS result_status;")
