"""Physician-portal patient search. Lab-request creation used to live here
too (a separate Supabase-REST implementation of `lab_request_service.py`'s
SQLAlchemy version) — consolidated away; physicians now go through
`lab_request_service.create_lab_request` directly (see changelog.md's
"Duplicate lab-request creation implementations" entry).
"""
import logging

from src.core.encryption import decryptPii
from src.core.supabase import supabase
from src.schemas.physician import PhysicianPatientItem

logger = logging.getLogger(__name__)

async def searchPatients(q: str) -> list[PhysicianPatientItem]:
    """Search patients by first/last name (case-insensitive substring match).

    Fetches up to 100 patient rows, decrypts each candidate's name fields in
    Python, then filters — since names are encrypted at rest and can't be
    matched in a SQL `WHERE` clause. Rows that fail to decrypt are skipped.

    Args:
        q: search text, matched against decrypted first/last name.

    Returns:
        Matching patients, decrypted, up to the 100-row fetch window.
    """
    result = await supabase.table("patients").select(
        "patient_id, patient_uid, first_name, middle_name, last_name, date_of_birth, sex"
    ).limit(100).execute()
    rows = result.data or []
    qLower = q.lower()

    items: list[PhysicianPatientItem] = []
    for row in rows:
        try:
            first = decryptPii(row["first_name"])
            last = decryptPii(row["last_name"])
        except Exception:
            logger.exception("PII decrypt failed for patient row %s", row["patient_id"])
            continue
        if qLower in first.lower() or qLower in last.lower():
            try:
                dob = decryptPii(row["date_of_birth"])
            except Exception:
                dob = ""
            items.append(PhysicianPatientItem(
                patientId=row["patient_id"],
                patientUid=row["patient_uid"],
                firstName=first,
                middleName=decryptPii(row["middle_name"]) if row.get("middle_name") else None,
                lastName=last,
                dateOfBirth=dob,
                sex=row.get("sex", "OTHER"),
            ))
    return items
