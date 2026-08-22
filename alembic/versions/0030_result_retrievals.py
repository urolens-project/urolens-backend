"""create result_retrievals table

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-27
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "result_retrievals",
        sa.Column(
            "retrieval_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_results.result_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "physician_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "retrieved_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ip_address", sa.Text(), nullable=True),
    )
    op.execute("ALTER TABLE result_retrievals DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE result_retrievals ENABLE ROW LEVEL SECURITY;")
    op.drop_table("result_retrievals")
