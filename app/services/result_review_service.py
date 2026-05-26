import asyncio
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import HTTPException, status

from app.config import SUPABASE_URL, SUPABASE_IMAGE_BUCKET
from app.db.supabase import supabase

_PHT = timezone(timedelta(hours=8))

_ALLOWED_STATUSES_FOR_ACTION = {"PENDING_SUPERVISOR_APPROVAL"}
_VALID_ESCALATION_PATHS = {"NOTIFY_PHYSICIAN", "FLAG_SENIOR_REVIEW", "MARK_CRITICAL"}


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


async def _require_pending(result_id: str) -> dict:
    res = await (
        supabase.table("analysis_results")
        .select("result_id, specimen_id, status, image_id")
        .eq("result_id", result_id)
        .execute()
    )
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")
    ar = rows[0]
    if ar["status"] not in _ALLOWED_STATUSES_FOR_ACTION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Action not allowed in status '{ar['status']}'.",
        )
    return ar


# ── Pending queue ─────────────────────────────────────────────────────────────

async def get_pending(page: int, page_size: int) -> dict:
    offset = (page - 1) * page_size

    # Count total
    count_res = await (
        supabase.table("analysis_results")
        .select("result_id", count="exact")
        .eq("status", "PENDING_SUPERVISOR_APPROVAL")
        .execute()
    )
    total = count_res.count or 0

    # Fetch page
    page_res = await (
        supabase.table("analysis_results")
        .select("result_id, specimen_id, status, confirmed_at")
        .eq("status", "PENDING_SUPERVISOR_APPROVAL")
        .order("confirmed_at", desc=False)
        .range(offset, offset + page_size - 1)
        .execute()
    )
    ar_rows = page_res.data or []
    if not ar_rows:
        return {"items": [], "total": total, "page": page, "page_size": page_size}

    specimen_ids = [r["specimen_id"] for r in ar_rows]

    # Batch load specimens
    spec_res = await (
        supabase.table("specimens")
        .select("specimen_id, patient_name, patient_uid, medtech_id")
        .in_("specimen_id", specimen_ids)
        .execute()
    )
    spec_map: dict[str, dict] = {r["specimen_id"]: r for r in (spec_res.data or [])}

    # Batch load patients (by patient_uid) for age/sex
    patient_uids = list({s["patient_uid"] for s in spec_map.values() if s.get("patient_uid")})
    pat_map: dict[str, dict] = {}
    if patient_uids:
        pat_res = await (
            supabase.table("patients")
            .select("patient_uid, date_of_birth, sex")
            .in_("patient_uid", patient_uids)
            .execute()
        )
        pat_map = {r["patient_uid"]: r for r in (pat_res.data or [])}

    # Batch load medtech usernames
    medtech_ids = list({s["medtech_id"] for s in spec_map.values() if s.get("medtech_id")})
    user_map: dict[str, str] = {}
    if medtech_ids:
        user_res = await (
            supabase.table("users")
            .select("user_id, username")
            .in_("user_id", medtech_ids)
            .execute()
        )
        user_map = {r["user_id"]: r.get("username", "") for r in (user_res.data or [])}

    items = []
    for ar in ar_rows:
        spec = spec_map.get(ar["specimen_id"], {})
        pat = pat_map.get(spec.get("patient_uid", ""), {})
        items.append({
            "result_id": ar["result_id"],
            "specimen_id": ar["specimen_id"],
            "patient_name": spec.get("patient_name", ""),
            "patient_age": _compute_age(pat.get("date_of_birth")),
            "patient_sex": pat.get("sex"),
            "medtech_name": user_map.get(spec.get("medtech_id", ""), ""),
            "confirmed_at": ar.get("confirmed_at"),
            "status": ar["status"],
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Full result detail ────────────────────────────────────────────────────────

async def get_full_result(result_id: str) -> dict:
    ar_res = await (
        supabase.table("analysis_results")
        .select(
            "result_id, specimen_id, image_id, ai_findings, flagged_anomalies, "
            "particle_classes, model_version, status, smart_diagnosis_unavailable, "
            "confirmed_at, confirmation_notes"
        )
        .eq("result_id", result_id)
        .execute()
    )
    rows = ar_res.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")
    ar = rows[0]

    # Load related data in parallel
    spec_task = supabase.table("specimens").select(
        "specimen_id, patient_name, patient_uid, medtech_id"
    ).eq("specimen_id", ar["specimen_id"]).execute()

    overrides_task = supabase.table("manual_overrides").select(
        "override_id, parameter_name, original_ai_value, corrected_value, rationale, overridden_at"
    ).eq("result_id", result_id).execute()

    review_task = supabase.table("result_reviews").select(
        "annotation_notes"
    ).eq("result_id", result_id).limit(1).execute()

    spec_res, overrides_res, review_res = await asyncio.gather(spec_task, overrides_task, review_task)

    spec = (spec_res.data or [{}])[0]

    # Patient info
    patient_uid = spec.get("patient_uid", "")
    pat: dict = {}
    if patient_uid:
        pat_res = await (
            supabase.table("patients")
            .select("patient_uid, first_name, last_name, date_of_birth, sex")
            .eq("patient_uid", patient_uid)
            .limit(1)
            .execute()
        )
        pat = (pat_res.data or [{}])[0] if pat_res else {}

    # Medtech name
    medtech_name = ""
    if spec.get("medtech_id"):
        u_res = await (
            supabase.table("users")
            .select("username")
            .eq("user_id", spec["medtech_id"])
            .limit(1)
            .execute()
        )
        medtech_name = ((u_res.data or [{}])[0] if u_res else {}).get("username", "")

    # Image URL
    image_url: Optional[str] = None
    if ar.get("image_id"):
        img_res = await (
            supabase.table("images")
            .select("storage_key")
            .eq("image_id", ar["image_id"])
            .limit(1)
            .execute()
        )
        image_url = _image_public_url(((img_res.data or [{}])[0] if img_res else {}).get("storage_key"))

    overrides = [
        {
            "override_id": str(r.get("override_id", "")),
            "parameter_name": r["parameter_name"],
            "original_ai_value": r["original_ai_value"],
            "corrected_value": r["corrected_value"],
            "rationale": r["rationale"],
            "overridden_at": r.get("overridden_at", ""),
        }
        for r in (overrides_res.data or [])
    ]

    latest_annotation = None
    if review_res.data:
        latest_annotation = review_res.data[0].get("annotation_notes")

    patient_name = f"{pat.get('first_name', '')} {pat.get('last_name', '')}".strip() or spec.get("patient_name", "")

    return {
        "result_id": ar["result_id"],
        "specimen_id": ar["specimen_id"],
        "patient_name": patient_name,
        "patient_age": _compute_age(pat.get("date_of_birth")),
        "patient_sex": pat.get("sex"),
        "medtech_name": medtech_name,
        "confirmed_at": ar.get("confirmed_at"),
        "confirmation_notes": ar.get("confirmation_notes"),
        "ai_findings": ar.get("ai_findings") or {},
        "flagged_anomalies": ar.get("flagged_anomalies") or {},
        "particle_classes": ar.get("particle_classes") or {},
        "model_version": ar.get("model_version", ""),
        "manual_overrides": overrides,
        "image_url": image_url,
        "smart_diagnosis_unavailable": ar.get("smart_diagnosis_unavailable", True),
        "status": ar["status"],
        "annotation_notes": latest_annotation,
    }


# ── Annotation ────────────────────────────────────────────────────────────────

async def save_annotation(result_id: str, user_id: str, annotation_notes: str) -> dict:
    # Verify result exists
    check = await (
        supabase.table("analysis_results")
        .select("result_id")
        .eq("result_id", result_id)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    now = datetime.now(_PHT).isoformat()

    # Upsert: update if row exists for this result, otherwise insert
    existing = await (
        supabase.table("result_reviews")
        .select("review_id")
        .eq("result_id", result_id)
        .eq("reviewed_by", user_id)
        .execute()
    )

    if existing.data:
        await (
            supabase.table("result_reviews")
            .update({"annotation_notes": annotation_notes, "updated_at": now})
            .eq("review_id", existing.data[0]["review_id"])
            .execute()
        )
    else:
        await (
            supabase.table("result_reviews")
            .insert({
                "result_id": result_id,
                "reviewed_by": user_id,
                "annotation_notes": annotation_notes,
                "created_at": now,
                "updated_at": now,
            })
            .execute()
        )

    return {"result_id": result_id, "annotation_notes": annotation_notes}


# ── Approve ───────────────────────────────────────────────────────────────────

async def approve_result(result_id: str, user_id: str, notes: Optional[str]) -> dict:
    await _require_pending(result_id)

    now = datetime.now(_PHT).isoformat()
    row: dict = {"result_id": result_id, "approved_by": user_id, "approved_at": now}
    if notes:
        row["notes"] = notes
    await supabase.table("result_approvals").insert(row).execute()
    await (
        supabase.table("analysis_results")
        .update({"status": "APPROVED", "updated_at": now})
        .eq("result_id", result_id)
        .execute()
    )

    return {"result_id": result_id, "status": "APPROVED", "approved_at": now}


# ── Return for correction ─────────────────────────────────────────────────────

async def return_result(result_id: str, user_id: str, reason: str) -> dict:
    await _require_pending(result_id)

    now = datetime.now(_PHT).isoformat()
    return_row: dict = {
        "result_id": result_id,
        "returned_by": user_id,
        "returned_at": now,
    }
    if reason:
        return_row["reason"] = reason
    await supabase.table("result_returns").insert(return_row).execute()
    await (
        supabase.table("analysis_results")
        .update({"status": "RETURNED_FOR_CORRECTION", "updated_at": now})
        .eq("result_id", result_id)
        .execute()
    )

    return {"result_id": result_id, "status": "RETURNED_FOR_CORRECTION", "returned_at": now}


# ── Escalate ──────────────────────────────────────────────────────────────────

async def escalate_result(
    result_id: str,
    user_id: str,
    escalation_path: str,
    escalation_note: Optional[str],
) -> dict:
    if escalation_path not in _VALID_ESCALATION_PATHS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid escalation_path '{escalation_path}'.",
        )

    await _require_pending(result_id)

    now = datetime.now(_PHT).isoformat()
    esc_row: dict = {
        "result_id": result_id,
        "escalated_by": user_id,
        "escalation_path": escalation_path,
        "escalated_at": now,
    }
    if escalation_note:
        esc_row["escalation_note"] = escalation_note
    await supabase.table("escalations").insert(esc_row).execute()
    await (
        supabase.table("analysis_results")
        .update({"status": "CRITICAL_ESCALATED", "updated_at": now})
        .eq("result_id", result_id)
        .execute()
    )

    return {
        "result_id": result_id,
        "status": "CRITICAL_ESCALATED",
        "escalation_path": escalation_path,
        "escalated_at": now,
    }
