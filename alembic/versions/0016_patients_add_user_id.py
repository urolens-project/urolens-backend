"""add user_id FK to patients table

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-26

Source: Migration 0016 — STORY-WEB-16 linkage of patient record to user account
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("patients",
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.user_id", ondelete="SET NULL"),
                  nullable=True, unique=True))


def downgrade():
    op.drop_column("patients", "user_id")
