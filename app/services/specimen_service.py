from datetime import datetime, timezone, timedelta

_PHT = timezone(timedelta(hours=8))

from fastapi import HTTPException, status

from app.db.supabase import supabase

_VALID_REJECTION_REASONS = {"INSUFFICIENT_VOLUME", "WRONG_CONTAINER", "UNLABELED", "OTHER"}


async def reject_specimen(
    specimen_id: str,
    user_id: str,
    reason_code: str,
    free_text_note: str | None,
) -> dict:
    if reason_code not in _VALID_REJECTION_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid rejection reason: {reason_code}.",
        )

    result = await (
        supabase.table("specimens")
        .select("specimen_id, status, medtech_id")
        .eq("specimen_id", specimen_id)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Specimen not found.")

    specimen = rows[0]

    if specimen["medtech_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Specimen is not assigned to you.",
        )
    if specimen["status"] == "REJECTED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Specimen is already rejected.",
        )

    rejected_at = datetime.now(_PHT).isoformat()
    await (
        supabase.table("specimens")
        .update({
            "status": "REJECTED",
            "rejection_reason": reason_code,
            "rejection_note": free_text_note,
            "rejected_at": rejected_at,
            "updated_at": rejected_at,
        })
        .eq("specimen_id", specimen_id)
        .execute()
    )

    return {"specimen_id": specimen_id, "status": "REJECTED", "rejected_at": rejected_at}
