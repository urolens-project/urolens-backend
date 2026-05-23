"""create consents table

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('consents',
        sa.Column('consent_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('patients.patient_id'), nullable=False),
        sa.Column('consent_process', sa.Boolean(), nullable=False),
        sa.Column('consent_storage', sa.Boolean(), nullable=False),
        sa.Column('consent_research', sa.Boolean(), nullable=False),
        sa.Column('recorded_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('recorded_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.user_id'), nullable=False),
    )
    op.execute("ALTER TABLE consents DISABLE ROW LEVEL SECURITY;")


def downgrade():
    op.execute("ALTER TABLE consents ENABLE ROW LEVEL SECURITY;")
    op.drop_table('consents')
