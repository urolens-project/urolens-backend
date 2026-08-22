"""Image Retake Service — T2.7 (Supabase implementation)

Handles the Retake flow: the MedTech discards the current image and the capture
screen re-opens so a new image can be uploaded.

Responsibilities
----------------
- Validate the image can be discarded (must be ACTIVE, not already DISCARDED/REPLACED).
- Mark the image as DISCARDED in Supabase.
- Emit the IMAGE_DISCARDED audit event to the audit_logs table.
- The AnalysisResult row remains intact — it will be updated when the new image
  is uploaded and inference runs again.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ..core.exceptions import ConflictError, NotFoundError
from ..core.supabase import supabase as sb

log = logging.getLogger(__name__)


class ImageRetakeService:
    """Handles the Retake flow: discarding the current image so a new one
    can be uploaded, per the module docstring above.
    """

    async def discard_and_retake(
        self,
        image_id: uuid.UUID,
        medtech_id: uuid.UUID,
        request: Any = None,
    ) -> dict:
        """Mark the image DISCARDED so the MedTech can submit a new one.

        Returns a dict with image_id, status, discarded_at.

        Raises:
        ------
        NotFoundError  — image not found
        ConflictError  — image is already DISCARDED or REPLACED
        """
        # ── 1. Fetch current image state ──────────────────────────────────────
        result = await (
            sb.table("images")
            .select("image_id, specimen_id, status")
            .eq("image_id", str(image_id))
            .execute()
        )
        rows = result.data or []

        if not rows:
            raise NotFoundError(f"Image {image_id} not found.")

        image = rows[0]

        if image["status"] == "DISCARDED":
            raise ConflictError("This image has already been discarded.")

        if image["status"] == "REPLACED":
            raise ConflictError(
                "This image has been superseded by a newer upload. "
                "Please upload a new image."
            )

        # ── 2. Mark image DISCARDED ───────────────────────────────────────────
        now_iso = datetime.now(UTC).isoformat()
        try:
            await (
                sb.table("images")
                .update({"status": "DISCARDED", "discarded_at": now_iso, "updated_at": now_iso})
                .eq("image_id", str(image_id))
                .execute()
            )
        except Exception:
            # Retry without updated_at if that column doesn't exist
            await (
                sb.table("images")
                .update({"status": "DISCARDED", "discarded_at": now_iso})
                .eq("image_id", str(image_id))
                .execute()
            )

        # ── 3. Write audit log (non-fatal) ────────────────────────────────────
        try:
            ip_address: str | None = None
            if request and hasattr(request, "client") and request.client:
                ip_address = request.client.host

            await sb.table("audit_logs").insert({
                "event_type": "IMAGE_DISCARDED",
                "entity_type": "image",
                "entity_id": str(image_id),
                "user_id": str(medtech_id),
                "ip_address": ip_address,
                "detail_json": json.dumps({"specimen_id": image["specimen_id"]}),
                "occurred_at": now_iso,
            }).execute()
        except Exception as exc:
            log.warning("Could not write IMAGE_DISCARDED audit log: %s", exc)

        return {"image_id": str(image_id), "status": "DISCARDED", "discarded_at": now_iso}
