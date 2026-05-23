from datetime import datetime, timezone

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

    confirmed_at = datetime.now(timezone.utc).isoformat()
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
