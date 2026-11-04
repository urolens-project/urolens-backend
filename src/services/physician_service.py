"""Physician-portal patient search. Lab-request creation used to live here
too (a separate Supabase-REST implementation of `lab_request_service.py`'s
SQLAlchemy version) — consolidated away; physicians now go through
`lab_request_service.create_lab_request` directly (see changelog.md's
"Duplicate lab-request creation implementations" entry).
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.encryption import decryptPii
from src.models.patient import Patient
from src.schemas.physician import PhysicianPatientItem

logger = logging.getLogger(__name__)

# Matches patient_service.PatientService's search convention (the same
# AsyncSession, patient_uid-only pattern this fix brings physician search
# in line with).
_SEARCH_LIMIT = 20
_SEARCH_MIN_QUERY_LENGTH = 3


async def searchPatients(db: AsyncSession, q: str) -> list[PhysicianPatientItem]:
    """Search patients by Patient ID (patient_uid) substring match only (UROLENS-152).

    Deliberately does NOT match on name: this search previously decrypted
    and substring-matched first/last name for up to 100 rows fetched via
    Supabase REST, and `patient_uid` was never part of the match condition
    at all — despite the UAC explicitly requiring Patient ID search (and
    the frontend's placeholder text already promising it), so a physician
    searching by Patient ID always got zero results. `patient_uid` isn't
    encrypted, so this filters in SQL directly via `AsyncSession` — no
    scan-then-decrypt-everything needed.

    Args:
        db: request-scoped `AsyncSession`.
        q: Case-insensitive substring to match against `patient_uid`. Below
            `_SEARCH_MIN_QUERY_LENGTH` chars, this is a no-op (empty result)
            rather than a broad/unfiltered scan — enforced here as well as
            by the route's `Query(min_length=...)`, since this function can
            be called directly, not only via HTTP.

    Returns:
        Up to `_SEARCH_LIMIT` matching patients. Each item carries only
        `patientId`/`patientUid`/`dateOfBirth`/`sex` — the two extra fields
        beyond the bare minimum are a stated UAC requirement, so the
        physician can visually confirm they've found the right person
        before selecting. `dateOfBirth` is decrypted only for this already
        patient_uid-filtered, small result set, never for a broad scan.
    """
    if len(q) < _SEARCH_MIN_QUERY_LENGTH:
        return []

    stmt = select(Patient).where(Patient.patientUid.ilike(f"%{q}%")).limit(_SEARCH_LIMIT)
    rows = (await db.execute(stmt)).scalars().all()

    items: list[PhysicianPatientItem] = []
    for row in rows:
        try:
            dob = decryptPii(row.dateOfBirth)
        except Exception:
            logger.exception("PII decrypt failed for patient row %s", row.patientId)
            continue
        items.append(PhysicianPatientItem(
            patientId=row.patientId,
            patientUid=row.patientUid,
            dateOfBirth=dob,
            sex=row.sex,
        ))
    return items
