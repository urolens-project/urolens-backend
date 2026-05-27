from datetime import datetime, timezone, timedelta

_PHT = timezone(timedelta(hours=8))

from fastapi import HTTPException, status

from app.db.supabase import supabase


async def confirm_result(
    result_id: str,
    user_id: str,
    notes: str | None,
) -> dict:
    # Fetch result
    result = await (
        supabase.table("analysis_results")
        .select("result_id, specimen_id, status")
        .eq("result_id", result_id)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    ar = rows[0]

    if ar["status"] != "PENDING_CONFIRM":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Result cannot be confirmed in status '{ar['status']}'.",
        )

    # Verify specimen belongs to the requesting medtech
    spec_result = await (
        supabase.table("specimens")
        .select("medtech_id")
        .eq("specimen_id", ar["specimen_id"])
        .execute()
    )
    spec_rows = spec_result.data or []
    if not spec_rows or spec_rows[0]["medtech_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Result does not belong to your specimens.",
        )

    confirmed_at = datetime.now(_PHT).isoformat()
    await (
        supabase.table("analysis_results")
        .update({
            "status": "PENDING_SUPERVISOR_APPROVAL",
            "confirmed_by": user_id,
            "confirmed_at": confirmed_at,
            "confirmation_notes": notes,
            "updated_at": confirmed_at,
        })
        .eq("result_id", result_id)
        .execute()
    )

    return {
        "result_id": result_id,
        "status": "PENDING_SUPERVISOR_APPROVAL",
        "confirmed_at": confirmed_at,
    }


async def get_smart_diagnosis(result_id: str) -> dict:
    # Check the result exists
    result = await (
        supabase.table("analysis_results")
        .select("result_id, smart_diagnosis_unavailable")
        .eq("result_id", result_id)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    if rows[0]["smart_diagnosis_unavailable"]:
        return {"result_id": result_id, "status": "FLAGGED_UNAVAILABLE"}

    # Fetch smart diagnosis output
    output = await (
        supabase.table("smart_diagnosis_outputs")
        .select("*")
        .eq("result_id", result_id)
        .execute()
    )
    output_rows = output.data or []

    if not output_rows or output_rows[0]["status"] == "FLAGGED_UNAVAILABLE":
        return {"result_id": result_id, "status": "FLAGGED_UNAVAILABLE"}

    row = output_rows[0]
    evidence_raw = row.get("evidence_map") or {}

    return {
        "output_id": str(row["output_id"]),
        "result_id": result_id,
        "status": "ATTACHED",
        "gout_score":   row["gout_score"],
        "gn_score":     row["gn_score"],
        "nephro_score": row["nephro_score"],
        "evidence_map": {
            "gout":               evidence_raw.get("gout", {}),
            "glomerulonephritis": evidence_raw.get("glomerulonephritis", {}),
            "nephrolithiasis":    evidence_raw.get("nephrolithiasis", {}),
        },
        "no_significant_indicators": row.get("no_significant_indicators", False),
        "engine_version": row.get("engine_version", ""),
        "generated_at": str(row.get("generated_at", "")),
    }


async def override_parameter(
    result_id: str,
    user_id: str,
    parameter_name: str,
    original_ai_value: str,
    corrected_value: str,
    rationale: str,
) -> dict:
    result = await (
        supabase.table("analysis_results")
        .select("result_id, specimen_id")
        .eq("result_id", result_id)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    spec_result = await (
        supabase.table("specimens")
        .select("medtech_id")
        .eq("specimen_id", rows[0]["specimen_id"])
        .execute()
    )
    spec_rows = spec_result.data or []
    if not spec_rows or spec_rows[0]["medtech_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Result does not belong to your specimens.",
        )

    now = datetime.now(_PHT).isoformat()
    insert_result = await (
        supabase.table("manual_overrides")
        .insert({
            "result_id": result_id,
            "parameter_name": parameter_name,
            "original_ai_value": original_ai_value,
            "corrected_value": corrected_value,
            "rationale": rationale,
            "overridden_by": user_id,
            "overridden_at": now,
        })
        .execute()
    )
    override_row = (insert_result.data or [{}])[0]

    return {
        "override_id": str(override_row.get("override_id", "")),
        "result_id": result_id,
        "parameter_name": parameter_name,
    }
