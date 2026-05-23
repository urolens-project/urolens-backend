"""create patients table

Revision ID: 0004
Revises:
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('patients',
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('patient_uid', sa.VARCHAR(20), unique=True, nullable=False),
        sa.Column('first_name', sa.Text(), nullable=False),
        sa.Column('last_name', sa.Text(), nullable=False),
        sa.Column('date_of_birth', sa.Text(), nullable=False),
        sa.Column('contact_no', sa.Text()),
        sa.Column('address', sa.Text()),
        sa.Column('is_walkin', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('record_flag', sa.VARCHAR(50)),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.execute("ALTER TABLE patients DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE patients ENABLE ROW LEVEL SECURITY;")
    op.drop_table('patients')
