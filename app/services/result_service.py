from fastapi import HTTPException, status

from src.urolens.core.supabase import supabase


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