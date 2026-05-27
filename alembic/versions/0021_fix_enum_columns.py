"""Convert USER-DEFINED enum columns to VARCHAR to match SQLAlchemy String models

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-27

Root cause: several PostgreSQL columns were created as custom enum types
(user_role, notification_type, probability_level) but their SQLAlchemy models
define them as String/VARCHAR. asyncpg cannot bind VARCHAR parameters to enum
columns — it raises DatatypeMismatchError or UndefinedFunctionError, which
aborts the PostgreSQL transaction and causes all subsequent operations to fail.

Columns fixed:
  users.role                   user_role         -> VARCHAR(30)
  notifications.type           notification_type -> VARCHAR(100), renamed to notification_type
  smart_diagnosis_outputs.gout_score    probability_level -> VARCHAR(10)
  smart_diagnosis_outputs.gn_score      probability_level -> VARCHAR(10)
  smart_diagnosis_outputs.nephro_score  probability_level -> VARCHAR(10)
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users.role: user_role enum -> VARCHAR(30)
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(30) USING role::text"
    )

    # notifications: rename type -> notification_type, convert enum -> VARCHAR(100)
    op.execute('ALTER TABLE notifications RENAME COLUMN "type" TO notification_type')
    op.execute(
        "ALTER TABLE notifications "
        "ALTER COLUMN notification_type TYPE VARCHAR(100) USING notification_type::text"
    )

    # smart_diagnosis_outputs score columns: probability_level enum -> VARCHAR(10)
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN gout_score TYPE VARCHAR(10) USING gout_score::text"
    )
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN gn_score TYPE VARCHAR(10) USING gn_score::text"
    )
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN nephro_score TYPE VARCHAR(10) USING nephro_score::text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role USING role::user_role"
    )
    op.execute(
        "ALTER TABLE notifications RENAME COLUMN notification_type TO type"
    )
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN type TYPE notification_type USING type::notification_type"
    )
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN gout_score TYPE probability_level USING gout_score::probability_level"
    )
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN gn_score TYPE probability_level USING gn_score::probability_level"
    )
    op.execute(
        "ALTER TABLE smart_diagnosis_outputs "
        "ALTER COLUMN nephro_score TYPE probability_level USING nephro_score::probability_level"
    )
