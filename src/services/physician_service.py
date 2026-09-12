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
    """Search patients by Patient ID (patient_uid) only — deliberately not by
    name, same reasoning as patient_service.searchPatients on the
    receptionist side: letting a physician type an arbitrary name to check
    whether that person has a record here at all is a real privacy leak.
    A physician attaching a lab request to a patient is expected to have
    that patient's ID on hand, not to browse by name.

    patient_uid isn't encrypted, so this filters server-side via Supabase's
    own `ilike`, rather than fetching a window of rows and matching in
    Python the way name-based matching had to.

    Args:
        q: search text, matched against patient_uid.

    Returns:
        Matching patients, decrypted, up to 20 results.
    """
    result = (
        await supabase.table("patients")
        .select("patient_id, patient_uid, first_name, middle_name, last_name, date_of_birth, sex")
        .ilike("patient_uid", f"%{q}%")
        .limit(20)
        .execute()
    )
    rows = result.data or []

    items: list[PhysicianPatientItem] = []
    for row in rows:
        try:
            first = decryptPii(row["first_name"])
            last = decryptPii(row["last_name"])
        except Exception:
            logger.exception("PII decrypt failed for patient row %s", row["patient_id"])
            continue
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
