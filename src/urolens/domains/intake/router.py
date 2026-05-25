from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from datetime import date, datetime
import random, logging

from src.urolens.core.database import supabase 

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/v1/intake",
    tags=["Patient Intake"]
)

REAL_USER_ID = "2c1c8ecb-b751-42ce-b372-a82e304b1a65"

class PatientRegistrationRequest(BaseModel):
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: date
    sex: str                    # was: gender
    contact_no: str | None = None    # was: contact_number
    address: str | None = None       # was: complete_address
    is_walkin: bool
    consent: dict | None = None

@router.get("/patients/search")
async def search_patients_endpoint(q: str):
    from src.urolens.core.encryption import decrypt_pii
    try:
        q_lower = q.strip().lower()

        # Patient UIDs are stored as plain text — search them directly.
        # Names are encrypted so ilike won't match; fetch all and filter in Python.
        response = await supabase.table("patients")\
            .select("patient_id, patient_uid, first_name, last_name")\
            .limit(200)\
            .execute()

        results = []
        for row in (response.data or []):
            try:
                first = decrypt_pii(row["first_name"])
                last = decrypt_pii(row["last_name"])
            except Exception:
                first = ""
                last = ""

            uid = row.get("patient_uid", "")
            if q_lower in first.lower() or q_lower in last.lower() or q_lower in uid.lower():
                results.append({
                    "patient_id": row["patient_id"],
                    "patient_uid": uid,
                    "first_name": first,
                    "last_name": last,
                })
            if len(results) == 5:
                break

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/patients", status_code=status.HTTP_201_CREATED)
async def register_patient_endpoint(payload: PatientRegistrationRequest):
    try:
        generated_sequence_id = f"URLNS-2026-{random.randint(10000, 99999)}"
        payload_data = payload.dict()
        
        incoming_sex = payload_data.get("sex", "OTHER")
        if incoming_sex:
            incoming_sex = incoming_sex.upper()
        
        patient_record_payload = {
            "patient_uid": generated_sequence_id,
            "first_name": payload_data.get("first_name"),
            "middle_name": payload_data.get("middle_name"),
            "last_name": payload_data.get("last_name"),
            "date_of_birth": str(payload_data.get("date_of_birth")),
            "sex": incoming_sex,
            "contact_no": payload_data.get("contact_no"),
            "address": payload_data.get("address"),
            "is_walkin": payload_data.get("is_walkin", False),
            "registered_by": REAL_USER_ID,
            "record_flag": "COMPLETE"
        }
        
        response = await supabase.table("patients").insert(patient_record_payload).execute()
        
        return {
            "success": True,
            "patient_id": generated_sequence_id,
            "message": "Patient registered successfully.",
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Supabase write failure: {str(e)}"
        )