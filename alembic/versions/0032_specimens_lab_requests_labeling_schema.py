"""specimens lab requests labeling schema

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-21 17:08:58.964355

Formalizes tables/columns that were created out-of-band before this migration
existed (specimens.medtech_id/patient_name/patient_uid/test_type/priority_level/
rejection_reason/rejection_note/rejected_at, and the lab_requests, sample_labels,
print_jobs, specimen_rejections tables) — confirmed live and in active use by
grepping every place that reads/writes them (app/services/specimen_service.py,
seed_specimens.py, src/urolens/services/queue_service.py, and the pre-merge
routers under src/urolens/domains/intake and src/urolens/domains/request).

Every statement uses IF NOT EXISTS / DO $$ ... EXCEPTION guards, matching the
pattern this repo's own migration_sql.sql already used for the same reason:
this environment could not confirm the live schema before authoring this file
(no network access to the real DB from the authoring sandbox), so every
statement is written to be a safe no-op if the table/column already exists
rather than fail. Whoever applies this against the real database should still
diff it against the actual live schema first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0032'
down_revision: Union[str, Sequence[str], None] = '0031'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── specimens: denormalized intake/labeling columns ─────────────────────
    # ADD COLUMN IF NOT EXISTS is atomic per-column: if the column already
    # exists, the whole clause (including its inline REFERENCES) is skipped,
    # so this can never attempt to add a duplicate FK constraint.
    op.execute(
        "ALTER TABLE specimens ADD COLUMN IF NOT EXISTS medtech_id UUID "
        "REFERENCES users(user_id) ON DELETE RESTRICT"
    )
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS patient_name TEXT")
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS patient_uid VARCHAR(30)")
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS test_type VARCHAR(50)")
    op.execute(
        "ALTER TABLE specimens ADD COLUMN IF NOT EXISTS priority_level VARCHAR(20) "
        "NOT NULL DEFAULT 'ROUTINE'"
    )
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS rejection_reason VARCHAR(30)")
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS rejection_note TEXT")
    op.execute("ALTER TABLE specimens ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ")

    # ── lab_requests ─────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_requests (
            lab_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            request_uid VARCHAR(30) NOT NULL UNIQUE,
            patient_id UUID NOT NULL REFERENCES patients(patient_id) ON DELETE RESTRICT,
            physician_id UUID REFERENCES users(user_id) ON DELETE SET NULL,
            physician_name VARCHAR(255),
            test_type VARCHAR(50) NOT NULL,
            clinical_notes TEXT,
            status VARCHAR(30) NOT NULL DEFAULT 'PENDING_SAMPLE',
            encoded_by UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # ── sample_labels ────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sample_labels (
            label_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            specimen_id UUID NOT NULL REFERENCES specimens(specimen_id) ON DELETE RESTRICT,
            sample_uid VARCHAR(30),
            label_content_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            generated_by UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            affixed_confirmed BOOLEAN NOT NULL DEFAULT false,
            affixed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sample_labels_specimen_id "
        "ON sample_labels (specimen_id)"
    )

    # ── print_jobs ───────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS print_jobs (
            print_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            label_id UUID NOT NULL REFERENCES sample_labels(label_id) ON DELETE RESTRICT,
            specimen_id UUID NOT NULL REFERENCES specimens(specimen_id) ON DELETE RESTRICT,
            status VARCHAR(20) NOT NULL DEFAULT 'SENT',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_print_jobs_label_id ON print_jobs (label_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_print_jobs_specimen_id ON print_jobs (specimen_id)"
    )

    # ── specimen_rejections ──────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS specimen_rejections (
            rejection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            specimen_id UUID NOT NULL REFERENCES specimens(specimen_id) ON DELETE RESTRICT,
            medtech_id UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            reason_code VARCHAR(30) NOT NULL,
            free_text_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_specimen_rejections_specimen_id "
        "ON specimen_rejections (specimen_id)"
    )

    # RLS stays disabled on all four new/touched tables, consistent with the
    # rest of this project — compensated for by the canonical auth dependency
    # now required on every route in this domain (see the consolidation plan's
    # RLS note). Not re-stated per table here since none of these ever had RLS
    # enabled (they were created out-of-band, before this project's RLS policy
    # existed either way).


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE IF EXISTS specimen_rejections")
    op.execute("DROP TABLE IF EXISTS print_jobs")
    op.execute("DROP TABLE IF EXISTS sample_labels")
    op.execute("DROP TABLE IF EXISTS lab_requests")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS rejected_at")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS rejection_note")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS rejection_reason")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS priority_level")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS test_type")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS patient_uid")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS patient_name")
    op.execute("ALTER TABLE specimens DROP COLUMN IF EXISTS medtech_id")
