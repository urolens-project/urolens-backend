"""create queue_assignments table

Revision ID: 0010
Revises: 0005
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('queue_assignments',
        sa.Column('assignment_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('specimen_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('specimens.specimen_id'), nullable=False),
        sa.Column('medtech_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('assigned_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('assigned_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('status', sa.VARCHAR(50), nullable=False, server_default='ACTIVE'),
    )
    op.execute("ALTER TABLE queue_assignments DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE queue_assignments ENABLE ROW LEVEL SECURITY;")
    op.drop_table('queue_assignments')
