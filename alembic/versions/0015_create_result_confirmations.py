"""0015 create result_confirmations table

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-27

TASK-MOB-09-1 | STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/alembic/versions/0015_create_result_confirmations.py
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "result_confirmations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "result_id",
            UUID(as_uuid=True),
            sa.ForeignKey("analysis_results.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "confirmed_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="PENDING_SUPERVISOR_APPROVAL",
        ),
        sa.Column(
            "smart_diagnosis_triggered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "is_synced",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    # Each result may only have one confirmation
    op.create_unique_constraint(
        "uq_result_confirmations_result_id",
        "result_confirmations",
        ["result_id"],
    )

    op.create_index(
        "ix_result_confirmations_confirmed_by",
        "result_confirmations",
        ["confirmed_by"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_result_confirmations_confirmed_by",
        table_name="result_confirmations",
    )
    op.drop_constraint(
        "uq_result_confirmations_result_id",
        table_name="result_confirmations",
    )
    op.drop_table("result_confirmations")