"""Physician-portal result listing/detail — Supabase-REST implementation,
scoped to results for patients the requesting physician has an associated lab
request for.
"""
import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from src.core.config import settings
from src.core.encryption import decryptPii
from src.core.supabase import supabase
from src.schemas.physician import (
    PhysicianResultDetail,
    PhysicianResultSummary,
    SmartDiagnosisDetail,
)

_PHT = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


def _computeAge(dobStr: str | None) -> int | None:
    # Computes age in whole years from an ISO date-of-birth string;
    # returns None if unset or unparseable.
    if not dobStr:
        return None
    try:
        dob = date.fromisoformat(dobStr[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _imagePublicUrl(storageKey: str | None) -> str | None:
    # Builds the public Supabase storage URL for a specimen image; returns
    # None if there's no storage key or no configured Supabase URL.
    if not storageKey or not settings.supabaseUrl:
        return None
    base = settings.supabaseUrl.rstrip("/")
    return f"{base}/storage/v1/object/public/{settings.supabaseImageBucket}/{storageKey}"


async def _getPhysicianPatientIds(physicianId: str) -> list[str]:
    # Distinct patient IDs from lab requests attributed to this physician —
    # the access-scoping set used by list_results/get_result_detail.
    res = await supabase.table("lab_requests").select("patient_id").eq(
        "physician_id", physicianId
    ).execute()
    return list({row["patient_id"] for row in (res.data or []) if row.get("patient_id")})


async def listResults(physicianId: str, page: int, pageSize: int) -> dict:
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
    patientIds = await _getPhysicianPatientIds(physicianId)
    if not patientIds:
        return {"items": [], "total": 0, "page": page, "page_size": pageSize}

    offset = (page - 1) * pageSize

    countRes, pageRes = await asyncio.gather(
        supabase.table("analysis_results")
            .select("result_id", count="exact")
            .in_("patient_id", patientIds)
            .execute(),
        supabase.table("analysis_results")
            .select("result_id, specimen_id, patient_id, status, confirmed_at, created_at")
            .in_("patient_id", patientIds)
            .order("created_at", desc=True)
            .range(offset, offset + pageSize - 1)
            .execute(),
    )
    total = countRes.count or 0
    arRows = pageRes.data or []
    if not arRows:
        return {"items": [], "total": total, "page": page, "page_size": pageSize}

    specimenIds = [r["specimen_id"] for r in arRows if r.get("specimen_id")]
    dbPatientIds = list({r["patient_id"] for r in arRows if r.get("patient_id")})

    specRes, patRes = await asyncio.gather(
        supabase.table("specimens").select("specimen_id, patient_uid, patient_name").in_(
            "specimen_id", specimenIds
        ).execute(),
        supabase.table("patients").select(
            "patient_id, patient_uid, first_name, last_name, date_of_birth, sex"
        ).in_("patient_id", dbPatientIds).execute(),
    )
    specMap = {r["specimen_id"]: r for r in (specRes.data or [])}
    patMap = {str(r["patient_id"]): r for r in (patRes.data or [])}

    items = []
    for ar in arRows:
        spec = specMap.get(ar.get("specimen_id", ""), {})
        pat = patMap.get(str(ar.get("patient_id", "")), {})

        try:
            first = decryptPii(pat["first_name"]) if pat.get("first_name") else ""
            last = decryptPii(pat["last_name"]) if pat.get("last_name") else ""
            dob = decryptPii(pat["date_of_birth"]) if pat.get("date_of_birth") else None
        except Exception:
            first = last = ""
            dob = None
        patientName = f"{first} {last}".strip() or spec.get("patient_name", "")

        items.append(PhysicianResultSummary(
            resultId=ar["result_id"],
            specimenId=ar.get("specimen_id", ""),
            patientName=patientName,
            patientUid=spec.get("patient_uid", pat.get("patient_uid", "")),
            patientAge=_computeAge(dob),
            patientSex=pat.get("sex"),
            status=ar["status"],
            confirmedAt=ar.get("confirmed_at"),
            createdAt=ar.get("created_at", ""),
        ))

    return {"items": items, "total": total, "page": page, "page_size": pageSize}


async def getResultDetail(
    resultId: str,
    physicianId: str,
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
    arRes = await supabase.table("analysis_results").select(
        "result_id, specimen_id, patient_id, image_id, ai_findings, flagged_anomalies, "
        "particle_classes, model_version, status, smart_diagnosis_unavailable, confirmed_at"
    ).eq("result_id", resultId).execute()
    rows = arRes.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")
    ar = rows[0]

    # Verify physician has access via their lab requests
    patientId = ar.get("patient_id")
    if patientId:
        physicianPatientIds = await _getPhysicianPatientIds(physicianId)
        if str(patientId) not in physicianPatientIds:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    # Parallel: specimen, smart diagnosis, annotation
    specTask = supabase.table("specimens").select(
        "specimen_id, patient_uid, patient_name, medtech_id"
    ).eq("specimen_id", ar["specimen_id"]).execute()

    sdoTask = supabase.table("smart_diagnosis_outputs").select("*").eq(
        "result_id", resultId
    ).execute()

    reviewTask = supabase.table("result_reviews").select(
        "annotation_notes"
    ).eq("result_id", resultId).limit(1).execute()

    specRes, sdoRes, reviewRes = await asyncio.gather(specTask, sdoTask, reviewTask)
    spec = (specRes.data or [{}])[0]

    # Patient info
    pat: dict = {}
    if patientId:
        patRes = await supabase.table("patients").select(
            "patient_id, patient_uid, first_name, last_name, date_of_birth, sex"
        ).eq("patient_id", str(patientId)).limit(1).execute()
        pat = (patRes.data or [{}])[0]

    # Medtech username
    medtechName: str | None = None
    if spec.get("medtech_id"):
        uRes = await supabase.table("users").select("username").eq(
            "user_id", spec["medtech_id"]
        ).limit(1).execute()
        medtechName = ((uRes.data or [{}])[0]).get("username")

    # Image URL
    imageUrl: str | None = None
    if ar.get("image_id"):
        imgRes = await supabase.table("images").select("storage_key").eq(
            "image_id", ar["image_id"]
        ).limit(1).execute()
        imageUrl = _imagePublicUrl(((imgRes.data or [{}])[0]).get("storage_key"))

    # Decrypt patient PII
    try:
        first = decryptPii(pat["first_name"]) if pat.get("first_name") else ""
        last = decryptPii(pat["last_name"]) if pat.get("last_name") else ""
        dob = decryptPii(pat["date_of_birth"]) if pat.get("date_of_birth") else None
        sex = pat.get("sex")
    except Exception:
        first = last = ""
        dob = sex = None
    patientName = f"{first} {last}".strip() or spec.get("patient_name", "")

    # Smart diagnosis
    smartDiagnosis: SmartDiagnosisDetail | None = None
    if not ar.get("smart_diagnosis_unavailable", True):
        sdoRows = sdoRes.data or []
        if sdoRows and sdoRows[0].get("status") == "ATTACHED":
            sdo = sdoRows[0]
            evidenceRaw = sdo.get("evidence_map") or {}
            smartDiagnosis = SmartDiagnosisDetail(
                goutScore=sdo.get("gout_score", "LOW"),
                gnScore=sdo.get("gn_score", "LOW"),
                nephroScore=sdo.get("nephro_score", "LOW"),
                utiScore=sdo.get("uti_score", "LOW"),
                trichoScore=sdo.get("tricho_score", "LOW"),
                evidenceMap=evidenceRaw,
                noSignificantIndicators=sdo.get("no_significant_indicators", False),
                engineVersion=sdo.get("engine_version", ""),
            )

    annotationNotes: str | None = None
    if reviewRes.data:
        annotationNotes = reviewRes.data[0].get("annotation_notes")

    # Log retrieval (best-effort)
    ipAddress = request.client.host if request.client else "unknown"
    retrievedAt = datetime.now(_PHT).isoformat()
    try:
        await supabase.table("result_retrievals").insert({
            "retrieval_id": str(uuid.uuid4()),
            "result_id": resultId,
            "physician_id": physicianId,
            "retrieved_at": retrievedAt,
            "ip_address": ipAddress,
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
            "entity_id": resultId,
            "user_id": physicianId,
            "ip_address": ipAddress,
            "detail_json": {},
        }).execute()
    except Exception:
        logger.exception("Failed to log audit entry.")
        pass

    return PhysicianResultDetail(
        resultId=ar["result_id"],
        specimenId=ar.get("specimen_id", ""),
        patientName=patientName,
        patientUid=spec.get("patient_uid", pat.get("patient_uid", "")),
        patientAge=_computeAge(dob),
        patientSex=sex,
        medtechName=medtechName,
        confirmedAt=ar.get("confirmed_at"),
        aiFindings=ar.get("ai_findings") or {},
        flaggedAnomalies=ar.get("flagged_anomalies") or {},
        particleClasses=ar.get("particle_classes") or {},
        modelVersion=ar.get("model_version", ""),
        smartDiagnosis=smartDiagnosis,
        imageUrl=imageUrl,
        status=ar["status"],
        annotationNotes=annotationNotes,
    )
