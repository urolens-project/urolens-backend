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
    # Fetch result including the denormalized smart_diagnosis JSONB column
    result = await (
        supabase.table("analysis_results")
        .select("result_id, smart_diagnosis_unavailable, smart_diagnosis")
        .eq("result_id", result_id)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    ar = rows[0]

    # Try the smart_diagnosis_outputs table first (authoritative source)
    output = await (
        supabase.table("smart_diagnosis_outputs")
        .select("*")
        .eq("result_id", result_id)
        .execute()
    )
    output_rows = output.data or []

    if output_rows and output_rows[0].get("status") != "FLAGGED_UNAVAILABLE":
        row = output_rows[0]
        evidence_raw = row.get("evidence_map") or {}
        return {
            "output_id": str(row["output_id"]),
            "result_id": result_id,
            "status": "ATTACHED",
            "gout_score":   row["gout_score"],
            "gn_score":     row["gn_score"],
            "nephro_score": row["nephro_score"],
            "evidence_map": evidence_raw,
            "no_significant_indicators": row.get("no_significant_indicators", False),
            "engine_version": row.get("engine_version", ""),
            "generated_at": str(row.get("generated_at", "")),
        }

    # Fall back to the denormalized JSONB on analysis_results
    smart_diag = ar.get("smart_diagnosis")
    if smart_diag:
        return {
            "output_id": result_id,
            "result_id": result_id,
            "status": "ATTACHED",
            "gout_score":   smart_diag.get("gout", {}).get("level", "LOW"),
            "gn_score":     smart_diag.get("glomerulonephritis", {}).get("level", "LOW"),
            "nephro_score": smart_diag.get("nephrolithiasis", {}).get("level", "LOW"),
            "evidence_map": {
                "gout":               smart_diag.get("gout", {}),
                "glomerulonephritis": smart_diag.get("glomerulonephritis", {}),
                "nephrolithiasis":    smart_diag.get("nephrolithiasis", {}),
            },
            "no_significant_indicators": smart_diag.get("no_significant_indicators", False),
            "engine_version": smart_diag.get("engine_version", ""),
            "generated_at": "",
        }

    return {"result_id": result_id, "status": "FLAGGED_UNAVAILABLE"}


async def override_parameter(
    result_id: str,
    user_id: str,
    role: str,
    parameter_name: str,
    original_ai_value,
    corrected_value,
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

    # Only enforce ownership check for medtechs, not supervisors
    if role.upper() != "SUPERVISOR":
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
            "original_ai_value": int(float(original_ai_value)),
            "corrected_value": int(float(corrected_value)),
            "rationale": rationale,
            "medtech_id": user_id,        # ← was overridden_by, now correct column name
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