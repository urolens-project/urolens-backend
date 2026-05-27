"""Convert analysis_results.status from result_status enum to VARCHAR

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-27

The SQLAlchemy model defines status as String(48) but the DB column was created
as a custom PostgreSQL enum type result_status. asyncpg refuses to bind VARCHAR
to an enum column, causing DatatypeMismatchError on every status update.
Converting to VARCHAR matches the model and lets the Python ResultStatus enum
be the sole source of truth.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE analysis_results "
        "ALTER COLUMN status TYPE VARCHAR(48) USING status::text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE analysis_results "
        "ALTER COLUMN status TYPE result_status USING status::result_status"
    )
