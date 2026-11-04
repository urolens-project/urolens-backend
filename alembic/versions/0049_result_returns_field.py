"""result_returns: the field and particle a supervisor's return points at

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-28 00:00:00.000000

Multi-field web lane (docs/web-lane-plan.md, Q11). A supervisor returning a result
can say which field and which particle the MedTech should look at again. The reason
text is stored unchanged; these two columns carry the pointer:

  * result_returns.field_id       UUID NULL, FK result_fields(field_id) ON DELETE RESTRICT
  * result_returns.particle_type  VARCHAR(60) NULL

Both are nullable, so every existing return stays valid and nothing reads them with
`fieldWorkflowEnabled` off. `ADD COLUMN IF NOT EXISTS`, and the foreign key and the
index guarded by name, make this safe to run on a database that already has them.
No new table, so no row-level security change (`result_returns` was covered by 0040).

Downgrade is for a scratch or staging database. It refuses while any return carries
a pointer, since dropping the columns would lose it.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: str | Sequence[str] | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the two columns, the foreign key and its index."""
    op.execute("ALTER TABLE result_returns ADD COLUMN IF NOT EXISTS field_id UUID")
    op.execute("ALTER TABLE result_returns ADD COLUMN IF NOT EXISTS particle_type VARCHAR(60)")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_result_returns_field') THEN
                ALTER TABLE result_returns ADD CONSTRAINT fk_result_returns_field
                    FOREIGN KEY (field_id) REFERENCES result_fields(field_id) ON DELETE RESTRICT;
            END IF;
        END
        $$;
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_result_returns_field_id ON result_returns (field_id)")


def downgrade() -> None:
    """Drop the pointer columns, refusing while any return carries one."""
    op.execute(
        """
        DO $$
        BEGIN
            -- Nested: the inner query names the columns, so it is only planned once
            -- they are known to exist.
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'result_returns' AND column_name = 'field_id'
            ) THEN
                IF EXISTS (
                    SELECT 1 FROM result_returns
                    WHERE field_id IS NOT NULL OR particle_type IS NOT NULL
                ) THEN
                    RAISE EXCEPTION '0049 downgrade refused: result_returns carries field pointers';
                END IF;
            END IF;
        END
        $$;
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_result_returns_field_id")
    op.execute("ALTER TABLE result_returns DROP CONSTRAINT IF EXISTS fk_result_returns_field")
    op.execute("ALTER TABLE result_returns DROP COLUMN IF EXISTS particle_type")
    op.execute("ALTER TABLE result_returns DROP COLUMN IF EXISTS field_id")
