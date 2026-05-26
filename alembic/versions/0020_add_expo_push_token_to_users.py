# urolens-backend/alembic/versions/0019_add_expo_push_token_to_users.py

"""add expo_push_token to users

Revision ID: 0019
Revises: 0018
Create Date: 2025-08-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "expo_push_token",
            sa.String(length=255),
            nullable=True,
            comment="Expo push token for EPIC-MOB-08 push notifications",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "expo_push_token")