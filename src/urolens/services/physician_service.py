"""Physician-portal patient search. Lab-request creation used to live here
too (a separate Supabase-REST implementation of `lab_request_service.py`'s
SQLAlchemy version) — consolidated away; physicians now go through
`lab_request_service.create_lab_request` directly (see changelog.md's
"Duplicate lab-request creation implementations" entry)."""
from src.urolens.core.encryption import decrypt_pii
from src.urolens.core.supabase import supabase
from src.urolens.schemas.physician import PhysicianPatientItem


async def search_patients(q: str) -> list[PhysicianPatientItem]:
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
    q_lower = q.lower()

    items: list[PhysicianPatientItem] = []
    for row in rows:
        try:
            first = decrypt_pii(row["first_name"])
            last = decrypt_pii(row["last_name"])
        except Exception:
            continue
        if q_lower in first.lower() or q_lower in last.lower():
            try:
                dob = decrypt_pii(row["date_of_birth"])
            except Exception:
                dob = ""
            items.append(PhysicianPatientItem(
                patient_id=row["patient_id"],
                patient_uid=row["patient_uid"],
                first_name=first,
                middle_name=decrypt_pii(row["middle_name"]) if row.get("middle_name") else None,
                last_name=last,
                date_of_birth=dob,
                sex=row.get("sex", "OTHER"),
            ))
    return items
