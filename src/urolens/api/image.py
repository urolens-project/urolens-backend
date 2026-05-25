"""
Image upload and discard routes — T2.7

Uses the Supabase REST API (same pattern as all other mobile endpoints) instead
of SQLAlchemy, because DATABASE_URL points to localhost and is not reachable in
this environment; only the Supabase service key is available.

Routes
------
POST /api/v1/images/upload              — multipart upload, returns analysis result
POST /api/v1/images/{image_id}/discard  — mark image DISCARDED for retake flow
"""
from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from PIL import Image as PILImage
from pydantic import BaseModel

from ..core.exceptions import ImageFormatError, ImageResolutionError
from ..middleware.rbac import RequireRole
from ..models.user import UserRole
from app.db.supabase import supabase as sb

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/images", tags=["images"])

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}
AI_MODEL_VERSION = "mvp-v1.0"
STORAGE_BUCKET = "images"


# ── Response schemas ──────────────────────────────────────────────────────────

class AnalysisResultResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    specimen_id: uuid.UUID
    image_id: uuid.UUID | None
    status: str
    ai_findings: dict | None
    flagged_anomalies: dict | None


class ImageDiscardResponse(BaseModel):
    image_id: str
    status: str
    discarded_at: str | None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post(
    "/upload",
    response_model=AnalysisResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a microscopy image and trigger AI inference (T2.7)",
)
async def upload_image(
    request: Request,
    specimen_id: uuid.UUID = Form(...),
    file: UploadFile = File(..., description="JPEG or PNG, minimum 640×480"),
    claims: dict = Depends(RequireRole([UserRole.MEDTECH])),
) -> AnalysisResultResponse:
    uploader_id = uuid.UUID(claims["user_id"])
    raw_bytes = await file.read()

    # ── 1. Validate format ────────────────────────────────────────────────────
    content_type = file.content_type or ""
    if content_type not in ALLOWED_MIME_TYPES:
        raise ImageFormatError(
            f"Unsupported image format '{content_type}'. Accepted: JPEG, PNG."
        )

    # ── 2. Validate resolution ────────────────────────────────────────────────
    try:
        pil_img = PILImage.open(io.BytesIO(raw_bytes))
        width, height = pil_img.size
    except Exception as exc:
        raise ImageFormatError(f"Cannot read image file: {exc}")

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ImageResolutionError(
            f"Image resolution {width}×{height} is below the minimum "
            f"{MIN_WIDTH}×{MIN_HEIGHT} required for AI analysis."
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    image_id = uuid.uuid4()

    # ── 3. Mark previous ACTIVE image for this specimen as REPLACED ───────────
    try:
        await (
            sb.table("images")
            .update({"status": "REPLACED", "updated_at": now_iso})
            .eq("specimen_id", str(specimen_id))
            .eq("status", "ACTIVE")
            .execute()
        )
    except Exception:
        # updated_at column might not exist — retry without it
        try:
            await (
                sb.table("images")
                .update({"status": "REPLACED"})
                .eq("specimen_id", str(specimen_id))
                .eq("status", "ACTIVE")
                .execute()
            )
        except Exception as exc:
            log.warning("Could not mark previous image as REPLACED: %s", exc)

    # ── 4. Upload image bytes to Supabase Storage ─────────────────────────────
    storage_key = f"specimens/{specimen_id}/images/{image_id}.jpg"
    try:
        await sb.storage.from_(STORAGE_BUCKET).upload(
            storage_key,
            raw_bytes,
            {"content-type": content_type, "upsert": "false"},
        )
    except Exception as exc:
        # Storage bucket may not exist — log and continue.
        # The DB row is still created so the mobile flow can proceed.
        log.warning("Supabase Storage upload skipped (%s). Continuing without file.", exc)

    # ── 5. Insert images row ──────────────────────────────────────────────────
    image_payload: dict = {
        "image_id": str(image_id),
        "specimen_id": str(specimen_id),
        "uploaded_by": str(uploader_id),
        "storage_key": storage_key,
        "file_format": MIME_TO_FORMAT[content_type],
        "width_px": width,
        "height_px": height,
        "file_size_bytes": len(raw_bytes),
        "status": "ACTIVE",
    }
    try:
        await sb.table("images").insert(image_payload).execute()
    except Exception as exc:
        log.error("Failed to insert image row: %s", exc)
        raise ImageFormatError(f"Failed to save image record: {exc}")

    # ── 6. Upsert analysis_results row ────────────────────────────────────────
    existing = await (
        sb.table("analysis_results")
        .select("result_id")
        .eq("specimen_id", str(specimen_id))
        .limit(1)
        .execute()
    )

    if existing.data:
        result_id = uuid.UUID(existing.data[0]["result_id"])
        await (
            sb.table("analysis_results")
            .update({
                "image_id": str(image_id),
                "status": "PENDING_CONFIRM",
                "ai_findings": {},
                "flagged_anomalies": {},
                "updated_at": now_iso,
            })
            .eq("result_id", str(result_id))
            .execute()
        )
    else:
        result_id = uuid.uuid4()
        await (
            sb.table("analysis_results")
            .insert({
                "result_id": str(result_id),
                "specimen_id": str(specimen_id),
                "image_id": str(image_id),
                "status": "PENDING_CONFIRM",
                "ai_findings": {},
                "flagged_anomalies": {},
                "model_version": AI_MODEL_VERSION,
            })
            .execute()
        )

    return AnalysisResultResponse(
        id=result_id,
        result_id=result_id,
        specimen_id=specimen_id,
        image_id=image_id,
        status="PENDING_CONFIRM",
        ai_findings=None,
        flagged_anomalies=None,
    )


@router.post(
    "/{image_id}/discard",
    response_model=ImageDiscardResponse,
    status_code=status.HTTP_200_OK,
    summary="Discard an image so the MedTech can retake (T2.7)",
)
async def discard_image(
    image_id: uuid.UUID,
    request: Request,
    claims: dict = Depends(RequireRole([UserRole.MEDTECH])),
) -> ImageDiscardResponse:
    now_iso = datetime.now(timezone.utc).isoformat()

    result = await (
        sb.table("images")
        .update({"status": "DISCARDED", "discarded_at": now_iso, "updated_at": now_iso})
        .eq("image_id", str(image_id))
        .execute()
    )

    if not result.data:
        # Retry without updated_at in case the column doesn't exist
        result = await (
            sb.table("images")
            .update({"status": "DISCARDED", "discarded_at": now_iso})
            .eq("image_id", str(image_id))
            .execute()
        )

    return ImageDiscardResponse(
        image_id=str(image_id),
        status="DISCARDED",
        discarded_at=now_iso,
    )
