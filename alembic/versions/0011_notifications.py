"""create notifications table

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('notifications',
        sa.Column('notification_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('notification_type', sa.VARCHAR(100), nullable=False),
        sa.Column('entity_id', postgresql.UUID(as_uuid=True)),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.execute("ALTER TABLE notifications DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;")
    op.drop_table('notifications')
