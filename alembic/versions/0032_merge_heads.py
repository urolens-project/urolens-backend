"""merge notifications branch (0011) with main chain (0031)

Revision ID: 0032
Revises: 0011, 0031
Create Date: 2026-05-28

The notifications table (0011) and the main epic-5/7/8 chain (0031) both
branch from 0005. This merge revision re-unifies both heads so that
'alembic upgrade head' works without the --heads disambiguation flag.
"""
from alembic import op

revision = "0032"
down_revision = ("0011", "0031")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
