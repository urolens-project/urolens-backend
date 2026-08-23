"""Physician-portal result listing/detail — Supabase-REST implementation,
scoped to results for patients the requesting physician has an associated lab
request for.
"""
import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from src.urolens.core.config import settings
from src.urolens.core.encryption import decrypt_pii
from src.urolens.core.supabase import supabase
from src.urolens.schemas.physician import (
    PhysicianResultDetail,
    PhysicianResultSummary,
    SmartDiagnosisDetail,
)

_PHT = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def _compute_age(dob_str: str | None) -> int | None:
    # Computes age in whole years from an ISO date-of-birth string;
    # returns None if unset or unparseable.
    if not dob_str:
        return None
    try:
        dob = date.fromisoformat(dob_str[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _image_public_url(storage_key: str | None) -> str | None:
    # Builds the public Supabase storage URL for a specimen image; returns
    # None if there's no storage key or no configured Supabase URL.
    if not storage_key or not settings.supabase_url:
        return None
    base = settings.supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{settings.supabase_image_bucket}/{storage_key}"


async def _get_physician_patient_ids(physician_id: str) -> list[str]:
    # Distinct patient IDs from lab requests attributed to this physician —
    # the access-scoping set used by list_results/get_result_detail.
    res = await supabase.table("lab_requests").select("patient_id").eq(
        "physician_id", physician_id
    ).execute()
    return list({row["patient_id"] for row in (res.data or []) if row.get("patient_id")})


async def list_results(physician_id: str, page: int, page_size: int) -> dict:
    """List analysis results for patients associated with this physician's
    lab requests, newest-created first.

    Args:
        physician_id: the authenticated physician; results are scoped to
            patients from this physician's own lab requests.
        page: 1-indexed page number.
        page_size: rows per page.

    Returns:
        A dict with `items` (list of `PhysicianResultSummary`), `total`
        (matching row count), `page`, and `page_size`.
    """
    patient_ids = await _get_physician_patient_ids(physician_id)
    if not patient_ids:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    offset = (page - 1) * page_size

    count_res, page_res = await asyncio.gather(
        supabase.table("analysis_results")
            .select("result_id", count="exact")
            .in_("patient_id", patient_ids)
            .execute(),
        supabase.table("analysis_results")
            .select("result_id, specimen_id, patient_id, status, confirmed_at, created_at")
            .in_("patient_id", patient_ids)
            .order("created_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute(),
    )
    total = count_res.count or 0
    ar_rows = page_res.data or []
    if not ar_rows:
        return {"items": [], "total": total, "page": page, "page_size": page_size}

    specimen_ids = [r["specimen_id"] for r in ar_rows if r.get("specimen_id")]
    db_patient_ids = list({r["patient_id"] for r in ar_rows if r.get("patient_id")})

    spec_res, pat_res = await asyncio.gather(
        supabase.table("specimens").select("specimen_id, patient_uid, patient_name").in_(
            "specimen_id", specimen_ids
        ).execute(),
        supabase.table("patients").select(
            "patient_id, patient_uid, first_name, last_name, date_of_birth, sex"
        ).in_("patient_id", db_patient_ids).execute(),
    )
    spec_map = {r["specimen_id"]: r for r in (spec_res.data or [])}
    pat_map = {str(r["patient_id"]): r for r in (pat_res.data or [])}

    items = []
    for ar in ar_rows:
        spec = spec_map.get(ar.get("specimen_id", ""), {})
        pat = pat_map.get(str(ar.get("patient_id", "")), {})

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
            patient_uid=spec.get("patient_uid", pat.get("patient_uid", "")),
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
    """Fetch one result's full detail for the physician portal, verifying
    the physician has access via their own lab requests, and logging the
    retrieval (both a `result_retrievals` row and an audit entry, best-effort).

    Args:
        physician_id: the authenticated physician; access is denied unless
            this physician has a lab request for the result's patient.

    Returns:
        A `PhysicianResultDetail` with patient info, findings, smart
        diagnosis (if attached), and image URL.

    Raises:
        HTTPException: 404, if `result_id` doesn't exist. 403, if the result's
            patient isn't among this physician's own patients.
    """
    ar_res = await supabase.table("analysis_results").select(
        "result_id, specimen_id, patient_id, image_id, ai_findings, flagged_anomalies, "
        "particle_classes, model_version, status, smart_diagnosis_unavailable, confirmed_at"
    ).eq("result_id", result_id).execute()
    rows = ar_res.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")
    ar = rows[0]

    # Verify physician has access via their lab requests
    patient_id = ar.get("patient_id")
    if patient_id:
        physician_patient_ids = await _get_physician_patient_ids(physician_id)
        if str(patient_id) not in physician_patient_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    # Parallel: specimen, smart diagnosis, annotation
    spec_task = supabase.table("specimens").select(
        "specimen_id, patient_uid, patient_name, medtech_id"
    ).eq("specimen_id", ar["specimen_id"]).execute()

    sdo_task = supabase.table("smart_diagnosis_outputs").select("*").eq(
        "result_id", result_id
    ).execute()

    review_task = supabase.table("result_reviews").select(
        "annotation_notes"
    ).eq("result_id", result_id).limit(1).execute()

    spec_res, sdo_res, review_res = await asyncio.gather(spec_task, sdo_task, review_task)
    spec = (spec_res.data or [{}])[0]

    # Patient info
    pat: dict = {}
    if patient_id:
        pat_res = await supabase.table("patients").select(
            "patient_id, patient_uid, first_name, last_name, date_of_birth, sex"
        ).eq("patient_id", str(patient_id)).limit(1).execute()
        pat = (pat_res.data or [{}])[0]

    # Medtech username
    medtech_name: str | None = None
    if spec.get("medtech_id"):
        u_res = await supabase.table("users").select("username").eq(
            "user_id", spec["medtech_id"]
        ).limit(1).execute()
        medtech_name = ((u_res.data or [{}])[0]).get("username")

    # Image URL
    image_url: str | None = None
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

    # Smart diagnosis
    smart_diagnosis: SmartDiagnosisDetail | None = None
    if not ar.get("smart_diagnosis_unavailable", True):
        sdo_rows = sdo_res.data or []
        if sdo_rows and sdo_rows[0].get("status") == "ATTACHED":
            sdo = sdo_rows[0]
            evidence_raw = sdo.get("evidence_map") or {}
            smart_diagnosis = SmartDiagnosisDetail(
                gout_score=sdo.get("gout_score", "LOW"),
                gn_score=sdo.get("gn_score", "LOW"),
                nephro_score=sdo.get("nephro_score", "LOW"),
                uti_score=sdo.get("uti_score", "LOW"),
                tricho_score=sdo.get("tricho_score", "LOW"),
                evidence_map=evidence_raw,
                no_significant_indicators=sdo.get("no_significant_indicators", False),
                engine_version=sdo.get("engine_version", ""),
            )

    annotation_notes: str | None = None
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
        logger.exception("Failed to log result retrieval.")
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
        logger.exception("Failed to log audit entry.")
        pass

    return PhysicianResultDetail(
        result_id=ar["result_id"],
        specimen_id=ar.get("specimen_id", ""),
        patient_name=patient_name,
        patient_uid=spec.get("patient_uid", pat.get("patient_uid", "")),
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
