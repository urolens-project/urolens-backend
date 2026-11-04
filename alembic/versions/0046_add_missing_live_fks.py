"""add the two live foreign keys that 0000 could not create

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-26 00:00:00.000000

Baseline 0000 creates `specimens` and `result_confirmations` before their FK
targets exist, so it leaves out two constraints that live has:

  * fk_specimens_lab_request   specimens.lab_request_id -> lab_requests
  * fk_confirmations_result    result_confirmations.result_id -> analysis_results

Names and ON DELETE behavior (NO ACTION) match docs/live-schema.json. Each is
added inside a DO block that checks pg_constraint by name, so this revision is
a no-op on live, where both already exist. Downgrade drops them with the same
guard.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0046'
down_revision: str | Sequence[str] | None = '0045'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_specimens_lab_request') THEN
                ALTER TABLE specimens ADD CONSTRAINT fk_specimens_lab_request
                    FOREIGN KEY (lab_request_id) REFERENCES lab_requests(lab_request_id);
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_confirmations_result') THEN
                ALTER TABLE result_confirmations ADD CONSTRAINT fk_confirmations_result
                    FOREIGN KEY (result_id) REFERENCES analysis_results(result_id);
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_confirmations_result') THEN
                ALTER TABLE result_confirmations DROP CONSTRAINT fk_confirmations_result;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_specimens_lab_request') THEN
                ALTER TABLE specimens DROP CONSTRAINT fk_specimens_lab_request;
            END IF;
        END
        $$;
        """
    )
