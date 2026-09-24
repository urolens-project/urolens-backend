"""patients dedup hash

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-24 00:00:00.000000

`PatientService._rejectIfDuplicate` previously decrypted and compared only
the first `_DUPLICATE_CHECK_LIMIT` (100) rows, unordered — a patient beyond
that scan window could never trigger the 409 duplicate check, and every
create paid an O(n) decrypt cost. This adds `dedup_hash`: an HMAC-SHA256
(keyed by `settings.dedupHashKey`, distinct from the Fernet
`ENCRYPTION_KEY`) of the normalized `first_name|last_name|date_of_birth`
triple, with a unique constraint, so the check becomes a single indexed
equality lookup with no row-count cap, and the DB itself is the last line
of defense against a race between two concurrent creates for the same
person.

A keyed hash (HMAC), not a plain SHA-256, is used deliberately: a plain hash
of a name+DOB triple is precomputable/dictionary-attackable (low entropy,
public-ish inputs), so a plain-hash column would let anyone who reads the
table run their own dictionary of candidate names/DOBs against it to
de-anonymize rows. Keying with a secret not in the table removes that.

This can't be pure SQL — PII is Fernet-encrypted client-side (per
`src/core/encryption.py`), so the values `dedup_hash` is derived from only
exist in plaintext momentarily in this migration's own Python process,
never written back to the DB in plaintext. `alembic/env.py` already imports
`src.core.config.settings` for `runMigrationsOnline`, so importing
`src.core.encryption`/`src.core.config` here follows the same existing
coupling, not a new one.

Sequencing, per the review comment this migration addresses: backfill
first, confirm zero collisions among existing rows, only then add the
`NOT NULL` + `UNIQUE` constraint — adding the constraint before confirming
no existing duplicates would abort the migration with a raw Postgres
constraint-violation error instead of a legible message naming the
conflicting patients.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0036'
down_revision: str | Sequence[str] | None = '0035'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _computeDedupHash(firstName: str, lastName: str, dateOfBirth: str) -> str:
    import hashlib
    import hmac

    from src.core.config import settings

    normalized = f"{firstName.strip().lower()}|{lastName.strip().lower()}|{dateOfBirth}"
    return hmac.new(
        settings.dedupHashKey.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def upgrade() -> None:
    """Upgrade schema."""
    from src.core.encryption import decryptPii

    op.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS dedup_hash VARCHAR(64)")

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT patient_id, patient_uid, first_name, last_name, date_of_birth FROM patients")
    ).fetchall()

    hashesByPatient: dict[str, str] = {}
    patientUidsByHash: dict[str, list[str]] = {}
    for patientId, patientUid, firstNameEnc, lastNameEnc, dobEnc in rows:
        firstName = decryptPii(firstNameEnc)
        lastName = decryptPii(lastNameEnc)
        dob = decryptPii(dobEnc)
        dedupHash = _computeDedupHash(firstName, lastName, dob)
        hashesByPatient[str(patientId)] = dedupHash
        patientUidsByHash.setdefault(dedupHash, []).append(patientUid)

    collisions = {h: uids for h, uids in patientUidsByHash.items() if len(uids) > 1}
    if collisions:
        details = "; ".join(f"{h[:8]}...: {uids}" for h, uids in collisions.items())
        raise RuntimeError(
            "Cannot add the patients.dedup_hash unique constraint: existing rows already "
            f"contain duplicate name+date-of-birth combinations ({details}). Resolve these "
            "patient records manually (merge or correct the data) and re-run this migration."
        )

    for patientId, dedupHash in hashesByPatient.items():
        bind.execute(
            sa.text("UPDATE patients SET dedup_hash = :dedupHash WHERE patient_id = :patientId"),
            {"dedupHash": dedupHash, "patientId": patientId},
        )

    op.execute("ALTER TABLE patients ALTER COLUMN dedup_hash SET NOT NULL")
    op.execute(
        "ALTER TABLE patients ADD CONSTRAINT uq_patients_dedup_hash UNIQUE (dedup_hash)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE patients DROP CONSTRAINT IF EXISTS uq_patients_dedup_hash")
    op.execute("ALTER TABLE patients DROP COLUMN IF EXISTS dedup_hash")
