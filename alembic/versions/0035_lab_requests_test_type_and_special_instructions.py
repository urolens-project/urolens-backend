"""lab requests test_type widen and special_instructions

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-25 00:00:00.000000

UROLENS-137. Two changes, bundled because both are driven by the same
`lab_request_service.createLabRequest` fix (removing the
`.upper().replace(" ", "_")` mangling of `testType` so it's stored as
entered):

1. `lab_requests.test_type` / `specimens.test_type` widened from
   VARCHAR(50) to VARCHAR(255). Verbatim storage of a free-typed "Other"
   test type can exceed 50 chars where the mangled/no-spaces version
   didn't; `specimens.test_type` is widened alongside it because
   `specimen_service.py` copies `lab_requests.test_type` into it verbatim
   at receiving time — leaving it at 50 would just move the truncation
   failure one hop downstream. `LabRequestCreateRequest.testType` (both
   `schemas/lab_request.py` and `schemas/physician.py` variants) now caps
   input at the same 255 chars via Pydantic, so an oversized value is a
   clean 422 rather than a DB-level DataError.
2. `lab_requests.special_instructions` (nullable TEXT) added — previously
   there was no column for this; the frontend merged it into
   `clinical_notes`.

NOT RUN against the live database — per the current task's constraint, and
because this repo's migration chain is already confirmed broken from a
truly empty database (chain root 0004 references a `users` table no
migration creates; see supabase-alembic-migration skill §1). Whoever
applies this should diff `test_type`'s live column width first, the same
caveat as migrations 0032/0033/0035(patient-intake branch) leave for
out-of-band schema drift.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0035'
down_revision: str | Sequence[str] | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE lab_requests ALTER COLUMN test_type TYPE VARCHAR(255)")
    op.execute("ALTER TABLE specimens ALTER COLUMN test_type TYPE VARCHAR(255)")
    op.execute("ALTER TABLE lab_requests ADD COLUMN IF NOT EXISTS special_instructions TEXT")


def downgrade() -> None:
    """Downgrade schema.

    Narrowing test_type back to VARCHAR(50) will fail if any row's value
    (verbatim-stored, post-upgrade) exceeds 50 chars — expected and
    intentional; a downgrade attempted after real verbatim data exists
    means truncating or deleting that data, which this migration will not
    silently do. Resolve the offending rows manually first.
    """
    op.execute("ALTER TABLE lab_requests DROP COLUMN IF EXISTS special_instructions")
    op.execute("ALTER TABLE specimens ALTER COLUMN test_type TYPE VARCHAR(50)")
    op.execute("ALTER TABLE lab_requests ALTER COLUMN test_type TYPE VARCHAR(50)")
