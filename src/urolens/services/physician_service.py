import random
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException, Request, status

from src.urolens.core.encryption import decrypt_pii
from src.urolens.core.supabase import supabase
from src.urolens.schemas.physician import (
    LabRequestCreateRequest,
    PhysicianPatientItem,
)

_PHT = timezone(timedelta(hours=8))


async def search_patients(q: str) -> list[PhysicianPatientItem]:
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


async def _generate_request_uid() -> str:
    date_str = datetime.now(_PHT).strftime("%Y%m%d")
    for _ in range(5):
        uid = f"REQ-{date_str}-{random.randint(10000, 99999)}"
        check = await supabase.table("lab_requests").select("request_uid").eq("request_uid", uid).execute()
        if not (check.data or []):
            return uid
    raise HTTPException(status_code=500, detail="Failed to generate unique request UID.")


async def create_lab_request(
    data: LabRequestCreateRequest,
    physician_id: str,
    physician_username: str,
    request: Request,
) -> dict:
    pat_res = await supabase.table("patients").select("patient_id").eq(
        "patient_id", str(data.patient_id)
    ).maybe_single().execute()
    if not pat_res.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found.")

    request_uid = await _generate_request_uid()
    now = datetime.now(_PHT).isoformat()

    payload = {
        "request_uid": request_uid,
        "patient_id": str(data.patient_id),
        "physician_id": physician_id,
        "physician_name": physician_username,
        "test_type": data.test_type.upper().replace(" ", "_"),
        "clinical_notes": data.clinical_notes,
        "status": "PENDING_SAMPLE",
        "encoded_by": physician_id,
        "created_at": now,
        "updated_at": now,
    }

    result = await supabase.table("lab_requests").insert(payload).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create lab request.")

    row = result.data[0]
    lab_request_id = row.get("lab_request_id")

    # Notify active receptionists (best-effort)
    try:
        rec_res = await supabase.table("users").select("user_id").eq(
            "role", "RECEPTIONIST"
        ).eq("is_active", True).execute()
        notifications = [
            {
                "notification_id": str(uuid.uuid4()),
                "user_id": rec["user_id"],
                "notification_type": "LAB_REQUEST_SUBMITTED",
                "message": f"New lab request {request_uid} submitted by Dr. {physician_username}.",
                "entity_id": str(lab_request_id) if lab_request_id else None,
                "entity_type": "lab_request",
            }
            for rec in (rec_res.data or [])
        ]
        if notifications:
            await supabase.table("notifications").insert(notifications).execute()
    except Exception:
        pass

    # Audit log (best-effort)
    ip_address = request.client.host if request.client else "unknown"
    try:
        await supabase.table("audit_logs").insert({
            "log_id": str(uuid.uuid4()),
            "event_type": "REQUEST_SUBMITTED",
            "entity_type": "lab_request",
            "entity_id": str(lab_request_id) if lab_request_id else str(uuid.uuid4()),
            "user_id": physician_id,
            "ip_address": ip_address,
            "detail_json": {"request_uid": request_uid, "patient_id": str(data.patient_id)},
        }).execute()
    except Exception:
        pass

    return {
        "request_uid": request_uid,
        "patient_id": data.patient_id,
        "physician_name": physician_username,
        "test_type": payload["test_type"],
        "status": "PENDING_SAMPLE",
        "created_at": now,
    }
