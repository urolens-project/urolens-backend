"""result_fields: the phone's capture id, so a retried upload doesn't add a second field

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-02 00:00:00.000000

The phone retries a field upload after a timeout or a 5xx. If the server had stored
the field but its answer was lost, the retry stored the same photo as a second field,
which counts that view twice in the averages (the "same view captured twice" bias in
the multi-field proposal, 3.3). The phone now names each photo once, when it is taken
or picked, and sends that name with every upload of it; `POST /fields` returns the
field it already made for a name it has seen on that specimen.

  * result_fields.client_capture_id  VARCHAR(64) NULL
  * uq_result_fields_specimen_capture  UNIQUE (specimen_id, client_capture_id)
    WHERE client_capture_id IS NOT NULL

Nullable: existing fields, and uploads from an app that sends no id, keep NULL and
behave as before. `IF NOT EXISTS` makes this safe to run twice. No new table, so no
row-level security change (`result_fields` was covered by 0047).

Downgrade drops the index and the column. The ids only matter while a retry may still
arrive, so nothing of lasting value is lost.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0050"
down_revision: str | Sequence[str] | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the capture id column and its per-specimen unique index."""
    op.execute("ALTER TABLE result_fields ADD COLUMN IF NOT EXISTS client_capture_id VARCHAR(64)")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_result_fields_specimen_capture
            ON result_fields (specimen_id, client_capture_id)
            WHERE client_capture_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the index and the column."""
    op.execute("DROP INDEX IF EXISTS uq_result_fields_specimen_capture")
    op.execute("ALTER TABLE result_fields DROP COLUMN IF EXISTS client_capture_id")
