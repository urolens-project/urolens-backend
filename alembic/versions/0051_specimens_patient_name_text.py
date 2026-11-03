"""specimens.patient_name: VARCHAR(255) -> TEXT

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-03 00:00:00.000000

The model maps specimens.patient_name as Text, but live (and a fresh build) has
VARCHAR(255). The name is stored Fernet-encrypted, and the ciphertext of a long or
accented name is far longer than the plain text, so a receive with a long name can
overrun 255 characters. Widening to TEXT removes the limit. Widening never rewrites
or loses a value, and running it on a column that is already TEXT changes nothing.

Downgrade narrows back to VARCHAR(255), but only if no stored value is longer than
255 characters; otherwise it raises, since narrowing would truncate or fail on data.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051"
down_revision: str | Sequence[str] | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen the column to TEXT."""
    op.execute("ALTER TABLE specimens ALTER COLUMN patient_name TYPE TEXT")


def downgrade() -> None:
    """Narrow back to VARCHAR(255), refusing if a value would not fit."""
    op.execute(
        """
        DO $$
        DECLARE longest integer;
        BEGIN
            SELECT max(length(patient_name)) INTO longest FROM specimens;
            IF longest > 255 THEN
                RAISE EXCEPTION '0051 cannot be downgraded: specimens.patient_name has a value of % characters (limit 255). Shorten or clear those values first.', longest;
            END IF;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE specimens ALTER COLUMN patient_name TYPE VARCHAR(255)")
