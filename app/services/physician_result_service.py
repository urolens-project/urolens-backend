import asyncio
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException, Request, status

from app.config import SUPABASE_URL, SUPABASE_IMAGE_BUCKET
from app.db.supabase import supabase
from app.schemas.physician import (
    PhysicianResultDetail,
    PhysicianResultListResponse,
    PhysicianResultSummary,
    SmartDiagnosisDetail,
)
from src.urolens.core.encryption import decrypt_pii

_PHT = timezone(timedelta(hours=8))


def _compute_age(dob_str: Optional[str]) -> Optional[int]:
    if not dob_str:
        return None
    try:
        dob = date.fromisoformat(dob_str[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _image_public_url(storage_key: Optional[str]) -> Optional[str]:
    if not storage_key or not SUPABASE_URL:
        return None
    base = SUPABASE_URL.rstrip("/")
    return f"{base}/storage/v1/object/public/{SUPABASE_IMAGE_BUCKET}/{storage_key}"


async def _get_physician_specimen_ids(physician_id: str) -> tuple[list[str], dict, dict]:
    """
    Returns (specimen_ids, spec_map, pat_by_uid) for the physician's patients.
    analysis_results has no patient_id; the join goes through specimens.patient_uid.
    """
    # Step 1: patient_ids from lab_requests
    lr_res = await supabase.table("lab_requests").select("patient_id").eq(
        "physician_id", physician_id
    ).execute()
    patient_ids = list({row["patient_id"] for row in (lr_res.data or []) if row.get("patient_id")})
    if not patient_ids:
        return [], {}, {}

    # Step 2: patient_uid for each patient_id
    pat_res = await supabase.table("patients").select(
        "patient_id, patient_uid, first_name, last_name, date_of_birth, sex"
    ).in_("patient_id", patient_ids).execute()
    pat_rows = pat_res.data or []
    pat_by_uid = {row["patient_uid"]: row for row in pat_rows}
    patient_uids = list(pat_by_uid.keys())
    if not patient_uids:
        return [], {}, {}

    # Step 3: specimen_ids for those patient_uids
    spec_res = await supabase.table("specimens").select(
        "specimen_id, patient_uid, patient_name"
    ).in_("patient_uid", patient_uids).execute()
    spec_rows = spec_res.data or []
    spec_map = {row["specimen_id"]: row for row in spec_rows}
    specimen_ids = list(spec_map.keys())

    return specimen_ids, spec_map, pat_by_uid


async def list_results(physician_id: str, page: int, page_size: int) -> dict:
    specimen_ids, spec_map, pat_by_uid = await _get_physician_specimen_ids(physician_id)
    if not specimen_ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    offset = (page - 1) * page_size

    count_res, page_res = await asyncio.gather(
        supabase.table("analysis_results")
            .select("result_id", count="exact")
            .in_("specimen_id", specimen_ids)
            .eq("status", "APPROVED")
            .execute(),
        supabase.table("analysis_results")
            .select("result_id, specimen_id, status, confirmed_at, created_at")
            .in_("specimen_id", specimen_ids)
            .eq("status", "APPROVED")
            .order("created_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute(),
    )
    total = count_res.count or 0
    ar_rows = page_res.data or []
    if not ar_rows:
        return {"items": [], "total": total, "page": page, "page_size": page_size}

    items = []
    for ar in ar_rows:
        spec = spec_map.get(ar.get("specimen_id", ""), {})
        patient_uid = spec.get("patient_uid", "")
        pat = pat_by_uid.get(patient_uid, {})

        try:
            first = decrypt_pii(pat["first_name"]) if pat.get("first_name") else ""
            last = decrypt_pii(pat["last_name"]) if pat.get("last_name") else ""
            dob = decrypt_pii(pat["date_of_birth"]) if pat.get("date_of_birth") else None
        except Exception:
            first = last = ""
            dob = None
        patient_name = f"{first} {last}".strip() or spec.get("patient_name", "")

        items.append(PhysicianResultSummary(
            result_id=ar["result_id"],
            specimen_id=ar.get("specimen_id", ""),
            patient_name=patient_name,
            patient_uid=patient_uid,
            patient_age=_compute_age(dob),
            patient_sex=pat.get("sex"),
            status=ar["status"],
            confirmed_at=ar.get("confirmed_at"),
            created_at=ar.get("created_at", ""),
        ))

    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_result_detail(
    result_id: str,
    physician_id: str,
    request: Request,
) -> dict:
    ar_res = await supabase.table("analysis_results").select(
        "result_id, specimen_id, image_id, ai_findings, flagged_anomalies, "
        "particle_classes, model_version, status, smart_diagnosis_unavailable, "
        "confirmed_at, smart_diagnosis"
    ).eq("result_id", result_id).execute()
    rows = ar_res.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")
    ar = rows[0]

    # Verify physician has access via specimens → patient_uid → lab_requests
    specimen_ids, spec_map, pat_by_uid = await _get_physician_specimen_ids(physician_id)
    if ar.get("specimen_id") not in specimen_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    spec = spec_map.get(ar.get("specimen_id", ""), {})
    patient_uid = spec.get("patient_uid", "")
    pat = pat_by_uid.get(patient_uid, {})

    # Parallel: medtech name, smart diagnosis outputs, annotation
    medtech_task = None
    if spec.get("medtech_id"):
        medtech_task = supabase.table("users").select("username").eq(
            "user_id", spec["medtech_id"]
        ).limit(1).execute()

    sdo_task = supabase.table("smart_diagnosis_outputs").select("*").eq(
        "result_id", result_id
    ).execute()

    review_task = supabase.table("result_reviews").select(
        "annotation_notes"
    ).eq("result_id", result_id).limit(1).execute()

    if medtech_task:
        medtech_res, sdo_res, review_res = await asyncio.gather(medtech_task, sdo_task, review_task)
        medtech_name = ((medtech_res.data or [{}])[0]).get("username")
    else:
        sdo_res, review_res = await asyncio.gather(sdo_task, review_task)
        medtech_name = None

    # Image URL
    image_url: Optional[str] = None
    if ar.get("image_id"):
        img_res = await supabase.table("images").select("storage_key").eq(
            "image_id", ar["image_id"]
        ).limit(1).execute()
        image_url = _image_public_url(((img_res.data or [{}])[0]).get("storage_key"))

    # Decrypt patient PII
    try:
        first = decrypt_pii(pat["first_name"]) if pat.get("first_name") else ""
        last = decrypt_pii(pat["last_name"]) if pat.get("last_name") else ""
        dob = decrypt_pii(pat["date_of_birth"]) if pat.get("date_of_birth") else None
        sex = pat.get("sex")
    except Exception:
        first = last = ""
        dob = sex = None
    patient_name = f"{first} {last}".strip() or spec.get("patient_name", "")

    # Smart diagnosis: prefer smart_diagnosis_outputs, fall back to JSONB column
    smart_diagnosis: Optional[SmartDiagnosisDetail] = None
    sdo_rows = sdo_res.data or []
    if sdo_rows and sdo_rows[0].get("status") == "ATTACHED":
        sdo = sdo_rows[0]
        smart_diagnosis = SmartDiagnosisDetail(
            gout_score=sdo.get("gout_score", "LOW"),
            gn_score=sdo.get("gn_score", "LOW"),
            nephro_score=sdo.get("nephro_score", "LOW"),
            evidence_map=sdo.get("evidence_map") or {},
            no_significant_indicators=sdo.get("no_significant_indicators", False),
            engine_version=sdo.get("engine_version", ""),
        )

    if smart_diagnosis is None:
        jsonb = ar.get("smart_diagnosis")
        if jsonb:
            smart_diagnosis = SmartDiagnosisDetail(
                gout_score=jsonb.get("gout", {}).get("level", "LOW"),
                gn_score=jsonb.get("glomerulonephritis", {}).get("level", "LOW"),
                nephro_score=jsonb.get("nephrolithiasis", {}).get("level", "LOW"),
                evidence_map={
                    "gout":               jsonb.get("gout", {}),
                    "glomerulonephritis": jsonb.get("glomerulonephritis", {}),
                    "nephrolithiasis":    jsonb.get("nephrolithiasis", {}),
                },
                no_significant_indicators=jsonb.get("no_significant_indicators", False),
                engine_version=jsonb.get("engine_version", ""),
            )

    annotation_notes: Optional[str] = None
    if review_res.data:
        annotation_notes = review_res.data[0].get("annotation_notes")

    # Log retrieval (best-effort)
    ip_address = request.client.host if request.client else "unknown"
    retrieved_at = datetime.now(_PHT).isoformat()
    try:
        await supabase.table("result_retrievals").insert({
            "retrieval_id": str(uuid.uuid4()),
            "result_id": result_id,
            "physician_id": physician_id,
            "retrieved_at": retrieved_at,
            "ip_address": ip_address,
        }).execute()
    except Exception:
        pass

    # Audit log (best-effort)
    try:
        await supabase.table("audit_logs").insert({
            "log_id": str(uuid.uuid4()),
            "event_type": "RESULT_RETRIEVED",
            "entity_type": "analysis_result",
            "entity_id": result_id,
            "user_id": physician_id,
            "ip_address": ip_address,
            "detail_json": {},
        }).execute()
    except Exception:
        pass

    return PhysicianResultDetail(
        result_id=ar["result_id"],
        specimen_id=ar.get("specimen_id", ""),
        patient_name=patient_name,
        patient_uid=patient_uid,
        patient_age=_compute_age(dob),
        patient_sex=sex,
        medtech_name=medtech_name,
        confirmed_at=ar.get("confirmed_at"),
        ai_findings=ar.get("ai_findings") or {},
        flagged_anomalies=ar.get("flagged_anomalies") or {},
        particle_classes=ar.get("particle_classes") or {},
        model_version=ar.get("model_version", ""),
        smart_diagnosis=smart_diagnosis,
        image_url=image_url,
        status=ar["status"],
        annotation_notes=annotation_notes,
    )
