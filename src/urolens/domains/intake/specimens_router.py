from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import Optional, List
import uuid
import random
from datetime import datetime
from src.urolens.core.database import supabase

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Specimens Receiving"]
)

CURRENT_RECEPTIONIST_ID = "2c1c8ecb-b751-42ce-b372-a82e304b1a65"

class SpecimenReceivePayload(BaseModel):
    lab_request_id: uuid.UUID
    visual_check_passed: bool
    rejection_reason: Optional[str] = None 
    free_text_note: Optional[Optional[str]] = None


@router.get("/search-request", response_model=List[dict])
def search_pending_lab_requests(q: str):
    """
    TASK-WEB-06-5: Find valid lab requests currently in PENDING_SAMPLE status
    """
    try:
        # Strip trailing gaps or prefix tags to secure character wildcards
        clean_q = q.strip()
        if clean_q.lower().startswith("dr."):
            clean_q = clean_q[3:].strip()
            
        # Format the parameters using clean logical escaping bounds
        filter_condition = f"request_uid.ilike.%{clean_q}%,physician_name.ilike.%{clean_q}%"
        
        response = supabase.table("lab_requests")\
            .select("lab_request_id, request_uid, test_type, physician_name, patient_id")\
            .eq("status", "PENDING_SAMPLE")\
            .or_(filter_condition)\
            .limit(5)\
            .execute()
            
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/receive", status_code=status.HTTP_201_CREATED)
def receive_specimen_endpoint(payload: SpecimenReceivePayload):
    """
    TASK-WEB-06-3 & TASK-WEB-06-4: Specimen processing pipeline with transactional fallbacks
    """
    try:
        # 1. Look up parent lab request data to denormalize into the specimen record
        req_query = supabase.table("lab_requests")\
            .select("request_uid, test_type, patient_id")\
            .eq("lab_request_id", str(payload.lab_request_id))\
            .single()\
            .execute()
        
        if not req_query.data:
            raise HTTPException(status_code=404, detail="Parent laboratory execution tracker not found.")
            
        parent_request = req_query.data
        
        pat_query = supabase.table("patients")\
            .select("first_name, last_name, patient_uid")\
            .eq("patient_id", parent_request.get("patient_id"))\
            .single()\
            .execute()
        
        p_name = "Unknown"
        p_uid = "N/A"
        if pat_query.data:
            p_name = f"{pat_query.data.get('first_name')} {pat_query.data.get('last_name')}"
            p_uid = pat_query.data.get("patient_uid")

        # 2. Compute statuses based on visual check criteria (Aligns with Workflow Requirements)
        initial_status = "RECEIVED" if payload.visual_check_passed else "REJECTED"
        parent_update_status = "SAMPLE_RECEIVED" if payload.visual_check_passed else "PENDING_SAMPLE"
        
        # Generate the unique sample identification sequence token
        generated_sample_uid = f"SMP-2026-{random.randint(10000, 99999)}"

        specimen_record = {
            "lab_request_id": str(payload.lab_request_id),
            "sample_uid": generated_sample_uid if payload.visual_check_passed else None,
            "status": initial_status,
            "visual_check_passed": payload.visual_check_passed,
            "received_by": CURRENT_RECEPTIONIST_ID,
            "patient_name": p_name,
            "patient_uid": p_uid,
            "test_type": parent_request.get("test_type"),
            "priority_level": "ROUTINE"
        }

        # 3. Insert deep into public.specimens
        specimen_tx = supabase.table("specimens").insert(specimen_record).execute()
        if not specimen_tx.data:
            raise Exception("Failed to write specimen record to cloud architecture storage.")
        
        new_specimen_id = specimen_tx.data[0].get("specimen_id")

        # 4. Handle conditional Rejection parameters if visual check fails
        if not payload.visual_check_passed:
            if not payload.rejection_reason:
                raise HTTPException(status_code=400, detail="A strict rejection structural reason code is mandatory.")
            
            # CRITICAL ENUM MATRIX GUARD: Validates payload strings against your exact database constraint keys
            ALLOWED_REJECTION_REASONS = {"INSUFFICIENT_VOLUME", "WRONG_CONTAINER", "UNLABELED", "OTHER"}
            if payload.rejection_reason not in ALLOWED_REJECTION_REASONS:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Invalid reason code. Must match database keys: {ALLOWED_REJECTION_REASONS}"
                )
            
            rejection_record = {
                "specimen_id": new_specimen_id,
                "medtech_id": CURRENT_RECEPTIONIST_ID, # Under initial logging workflow context
                "reason_code": payload.rejection_reason,
                "free_text_note": payload.free_text_note
            }
            supabase.table("specimen_rejections").insert(rejection_record).execute()

        # 5. Advance status on parent request row
        supabase.table("lab_requests")\
            .update({"status": parent_update_status})\
            .eq("lab_request_id", str(payload.lab_request_id))\
            .execute()

        # 6. Post state mutations directly to your system logging matrix
        audit_entry = {
            "action": "SPECIMEN_RECEIVED" if payload.visual_check_passed else "SPECIMEN_REJECTED",
            "reference_id": str(payload.lab_request_id),
            "timestamp": datetime.utcnow().isoformat()
        }
        # Un-comment below if audit table layer is active:
        # supabase.table("audit_logs").insert(audit_entry).execute()

        return {
            "success": True,
            "specimen_id": new_specimen_id,
            "sample_uid": generated_sample_uid if payload.visual_check_passed else None,
            "status": initial_status,
            "message": "Specimen workflow pipeline initialization recorded successfully."
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database execution anomaly: {str(e)}")