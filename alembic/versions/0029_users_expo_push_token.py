"""add expo_push_token to users

Revision ID: 0029
Revises: 0026
Create Date: 2026-05-27

Source: Migration 0029 — EPIC-MOB-08 Push Notifications
Notes: Stores per-user Expo push token so the backend can deliver
       push notifications via the Expo Push API.
"""
from alembic import op

revision = "0029"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS expo_push_token TEXT DEFAULT NULL;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE users
            DROP COLUMN IF EXISTS expo_push_token;
    """)
