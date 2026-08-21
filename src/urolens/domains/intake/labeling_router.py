from fastapi import APIRouter, HTTPException, status, Depends, Body
from pydantic import BaseModel
from typing import Optional
import uuid
import logging
from datetime import datetime

from app.middleware.rbac import RequireRole
from src.urolens.core.database import supabase
from src.urolens.core.enums import UserRole

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Sample Labeling Tracking"]
)

_medtech = RequireRole([UserRole.MEDTECH])


class ConfirmPayload(BaseModel):
    offline_override: bool = False


@router.get("/search-received")
async def search_received_specimens(q: str, current_user: dict = Depends(_medtech)):
    from src.urolens.core.encryption import decrypt_pii
    try:
        q_lower = q.strip().lower()

        response = await supabase.table("specimens")\
            .select("specimen_id, sample_uid, patient_name, patient_uid, test_type, status")\
            .eq("status", "RECEIVED")\
            .limit(200)\
            .execute()

        results = []
        for row in (response.data or []):
            try:
                name = decrypt_pii(row["patient_name"])
            except Exception:
                name = row.get("patient_name", "")

            uid = row.get("patient_uid", "")
            sample_uid = row.get("sample_uid", "")

            if q_lower in name.lower() or q_lower in uid.lower() or q_lower in sample_uid.lower():
                results.append({
                    "specimen_id": row["specimen_id"],
                    "sample_uid": sample_uid,
                    "patient_name": name,
                    "patient_uid": uid,
                    "test_type": row.get("test_type"),
                    "status": row.get("status"),
                })
            if len(results) == 5:
                break

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{id}/label", status_code=status.HTTP_201_CREATED)
async def generate_specimen_label_endpoint(
    id: uuid.UUID,
    current_user: dict = Depends(_medtech),
):
    try:
        operator_id = uuid.UUID(current_user["user_id"])

        spec_query = await supabase.table("specimens").select("*").eq("specimen_id", str(id)).single().execute()
        if not spec_query.data:
            raise HTTPException(status_code=404, detail="Specimen record not found.")

        specimen = spec_query.data
        if specimen.get("status") != "RECEIVED":
            raise HTTPException(status_code=400, detail=f"Specimen is in state '{specimen.get('status')}'. Must be RECEIVED.")

        from src.urolens.core.encryption import decrypt_pii
        try:
            patient_name = decrypt_pii(specimen.get("patient_name", ""))
        except Exception:
            patient_name = specimen.get("patient_name", "")

        label_content_json = {
            "patient_name": patient_name,
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
        label_tx = await supabase.table("sample_labels").insert(label_record).execute()
        if not label_tx.data:
            raise Exception("Label insert failed.")

        new_label_id = label_tx.data[0].get("label_id")

        print_job_record = {
            "label_id": new_label_id,
            "specimen_id": str(id),
            "status": "SENT"
        }
        print_tx = await supabase.table("print_jobs").insert(print_job_record).execute()
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
async def confirm_label_affixed_endpoint(
    id: uuid.UUID,
    current_user: dict = Depends(_medtech),
    payload: ConfirmPayload = Body(...),
):
    try:
        operator_id = current_user["user_id"]

        label_query = await supabase.table("sample_labels")\
            .select("label_id")\
            .eq("specimen_id", str(id))\
            .execute()

        if not label_query.data:
            if not payload.offline_override:
                raise HTTPException(
                    status_code=400,
                    detail="No label found. Print label first, or enable offline override."
                )

            spec_query = await supabase.table("specimens")\
                .select("*")\
                .eq("specimen_id", str(id))\
                .single()\
                .execute()

            if not spec_query.data:
                raise HTTPException(status_code=404, detail="Specimen not found.")

            specimen = spec_query.data
            from src.urolens.core.encryption import decrypt_pii
            try:
                offline_patient_name = decrypt_pii(specimen.get("patient_name", ""))
            except Exception:
                offline_patient_name = specimen.get("patient_name", "")

            offline_label = {
                "specimen_id": str(id),
                "sample_uid": specimen.get("sample_uid"),
                "label_content_json": {
                    "patient_name": offline_patient_name,
                    "patient_uid": specimen.get("patient_uid"),
                    "sample_uid": specimen.get("sample_uid"),
                    "test_type": specimen.get("test_type"),
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "offline_override": True
                },
                "generated_by": operator_id
            }
            label_tx = await supabase.table("sample_labels").insert(offline_label).execute()
            if not label_tx.data:
                raise Exception("Offline label insert failed.")

            target_label_id = label_tx.data[0]["label_id"]
            logger.warning(f"Offline override used for specimen {id}. Label: {target_label_id}")

        else:
            target_label_id = label_query.data[0].get("label_id")
            if not target_label_id:
                raise HTTPException(status_code=400, detail="Label record exists but label_id is null.")

        await supabase.table("specimens")\
            .update({"status": "LABELED"})\
            .eq("specimen_id", str(id))\
            .execute()

        await supabase.table("sample_labels").update({
            "affixed_confirmed": True,
            "affixed_at": datetime.utcnow().isoformat()
        }).eq("label_id", target_label_id).execute()

        return {
            "success": True,
            "message": "Specimen successfully advanced to LABELED status.",
            "updated_status": "LABELED",
            "offline_override_used": payload.offline_override
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Confirm failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Database assertion failure: {str(e)}")
