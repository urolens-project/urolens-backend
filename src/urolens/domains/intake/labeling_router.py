from fastapi import APIRouter, HTTPException, status, Depends, Header, Body
from pydantic import BaseModel
from typing import Optional
import uuid
import logging
from datetime import datetime
from src.urolens.core.database import supabase

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Sample Labeling Tracking"]
)

def get_current_user_id(x_user_id: Optional[str] = Header(None)) -> uuid.UUID:
    fallback_id = "2c1c8ecb-b751-42ce-b372-a82e304b1a65"
    target_id = x_user_id or fallback_id
    try:
        return uuid.UUID(target_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed context header: User identity cannot be evaluated structurally."
        )

# ← ADD THIS
class ConfirmPayload(BaseModel):
    offline_override: bool = False


@router.get("/search-received")
def search_received_specimens(q: str):
    try:
        response = supabase.table("specimens")\
            .select("specimen_id, sample_uid, patient_name, patient_uid, test_type, status")\
            .eq("status", "RECEIVED")\
            .ilike("patient_name", f"%{q}%")\
            .limit(5)\
            .execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{id}/label", status_code=status.HTTP_201_CREATED)
def generate_specimen_label_endpoint(
    id: uuid.UUID,
    operator_id: uuid.UUID = Depends(get_current_user_id)
):
    try:
        spec_query = supabase.table("specimens").select("*").eq("specimen_id", str(id)).single().execute()
        if not spec_query.data:
            raise HTTPException(status_code=404, detail="Specimen record not found.")

        specimen = spec_query.data
        if specimen.get("status") != "RECEIVED":
            raise HTTPException(status_code=400, detail=f"Specimen is in state '{specimen.get('status')}'. Must be RECEIVED.")

        label_content_json = {
            "patient_name": specimen.get("patient_name"),
            "patient_uid": specimen.get("patient_uid"),
            "sample_uid": specimen.get("sample_uid"),
            "test_type": specimen.get("test_type"),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        label_record = {
            "specimen_id": str(id),
            "sample_uid": specimen.get("sample_uid"),
            "label_content_json": label_content_json,
            "generated_by": str(operator_id)
        }
        label_tx = supabase.table("sample_labels").insert(label_record).execute()
        if not label_tx.data:
            raise Exception("Label insert failed.")

        new_label_id = label_tx.data[0].get("label_id")

        print_job_record = {
            "label_id": new_label_id,
            "specimen_id": str(id),
            "status": "SENT"
        }
        print_tx = supabase.table("print_jobs").insert(print_job_record).execute()
        if not print_tx.data:
            raise Exception("Print job insert failed.")

        return {
            "success": True,
            "label_id": new_label_id,
            "print_job_id": print_tx.data[0].get("print_job_id"),
            "preview": label_content_json
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database pipeline anomaly: {str(e)}")


@router.post("/{id}/label/confirm")
def confirm_label_affixed_endpoint(
    id: uuid.UUID,
    payload: ConfirmPayload = Body(...)
):
    try:
        label_query = supabase.table("sample_labels")\
            .select("label_id")\
            .eq("specimen_id", str(id))\
            .execute()

        if not label_query.data:
            raise HTTPException(status_code=400, detail="Safety Constraint Violation: Barcode print transaction trace must be executed before labels can be physically affixed.")

        target_label_id = label_query.data[0].get("label_id")

        # 2. Advance the local specimen container tracking state to 'LABELED'
        # Crucial Note: Check your check constraints. If your DB has constraints on specimens table, 'LABELED' matches standard setup templates
        spec_update = supabase.table("specimens").update({"status": "LABELED"}).eq("specimen_id", str(id)).execute()
        if not spec_update.data:
            raise Exception("Failure advancing state index trace inside public.specimens container.")

        # 3. Complete verification ticks on your labels subledger table row
        supabase.table("sample_labels").update({
            "affixed_confirmed": True,
            "affixed_at": datetime.utcnow().isoformat()
        }).eq("label_id", target_label_id).execute()

        # 4. Post state transformations straight to internal system audit logging logs
        audit_entry = {
            "action": "LABEL_AFFIXED",
            "reference_id": str(id),
            "timestamp": datetime.utcnow().isoformat()
        }
        # Un-comment if audit logs tracking tables are online:
        # supabase.table("audit_logs").insert(audit_entry).execute()

        return {
            "success": True,
            "message": "Specimen successfully advanced to LABELED status step tracker line items.",
            "updated_status": "LABELED"
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Confirm failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Database assertion failure: {str(e)}")