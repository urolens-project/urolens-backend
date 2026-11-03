"""specimen_rejections / print_jobs / sample_labels: created_at -> the names live uses

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-03 00:00:00.000000

Live has no created_at on these three tables; it has rejected_at, dispatched_at and
generated_at, and the models map those names. A database built from the migrations
(0000) still has created_at, so a fresh build did not match live or the models.

    specimen_rejections.created_at -> rejected_at
    print_jobs.created_at          -> dispatched_at
    sample_labels.created_at       -> generated_at

Each rename runs only if created_at exists and the new name does not. On live every
column is already named as above, so this migration changes nothing there.

Downgrade is a no-op: reversing the rename would break live, where the new names are
the original ones, and the migration cannot tell the two databases apart afterwards.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0052"
down_revision: str | Sequence[str] | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = (
    ("specimen_rejections", "created_at", "rejected_at"),
    ("print_jobs", "created_at", "dispatched_at"),
    ("sample_labels", "created_at", "generated_at"),
)


def upgrade() -> None:
    """Rename created_at where it still exists."""
    for table, old, new in RENAMES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = '{table}' AND column_name = '{old}')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                            WHERE table_schema = 'public' AND table_name = '{table}' AND column_name = '{new}') THEN
                    ALTER TABLE {table} RENAME COLUMN {old} TO {new};
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    """No-op; see the docstring."""
