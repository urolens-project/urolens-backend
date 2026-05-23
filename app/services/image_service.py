from datetime import datetime, timezone, timedelta

_PHT = timezone(timedelta(hours=8))

from fastapi import HTTPException, status

from app.db.supabase import supabase


async def discard_image(image_id: str, user_id: str) -> dict:
    image_result = await (
        supabase.table("images")
        .select("image_id, specimen_id, status")
        .eq("image_id", image_id)
        .execute()
    )
    rows = image_result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

    image = rows[0]

    if image["status"] == "DISCARDED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Image is already discarded.",
        )

    spec_result = await (
        supabase.table("specimens")
        .select("medtech_id")
        .eq("specimen_id", image["specimen_id"])
        .execute()
    )
    spec_rows = spec_result.data or []
    if not spec_rows or spec_rows[0]["medtech_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Image does not belong to your specimens.",
        )

    discarded_at = datetime.now(_PHT).isoformat()
    await (
        supabase.table("images")
        .update({"status": "DISCARDED", "discarded_at": discarded_at, "updated_at": discarded_at})
        .eq("image_id", image_id)
        .execute()
    )

    return {"image_id": image_id, "status": "DISCARDED"}
