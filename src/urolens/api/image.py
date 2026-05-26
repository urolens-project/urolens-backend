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

import asyncio
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
from ..services.image_retake_service import ImageRetakeService
from app.db.supabase import supabase as sb

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/images", tags=["images"])
_retake_service = ImageRetakeService()

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}
AI_MODEL_VERSION = "mvp-v1.0"
STORAGE_BUCKET = "images"


# ── AI inference hook ────────────────────────────────────────────────────────

async def _try_run_inference(
    raw_bytes: bytes,
    result_id: uuid.UUID,
    now_iso: str,
) -> dict | None:
    """
    Attempt AI inference via the urolens_ai package.

    When the AI engineer delivers the package, installing it activates inference
    automatically — no other code change required.

    Returns the findings dict on success, None when the package is absent or
    inference fails (the analysis_results row stays PENDING_CONFIRM in either case).
    """
    try:
        from urolens_ai import infer  # type: ignore[import]
    except ImportError:
        return None

    try:
        result = infer(raw_bytes)
        findings: dict = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        await (
            sb.table("analysis_results")
            .update({"ai_findings": findings, "updated_at": now_iso})
            .eq("result_id", str(result_id))
            .execute()
        )
        return findings
    except Exception as exc:
        log.warning("AI inference failed for result %s: %s", result_id, exc)
        return None


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
    # PIL.Image.open is synchronous/blocking — run in a thread to avoid
    # stalling the event loop on large images.
    try:
        def _read_dimensions() -> tuple[int, int]:
            img = PILImage.open(io.BytesIO(raw_bytes))
            return img.size

        width, height = await asyncio.to_thread(_read_dimensions)
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

    # ── 4. Storage path (upload deferred until AI integration) ───────────────
    # The storage_key is saved to DB now so it can be used later.
    # Actual file upload is skipped here: the 20 s Supabase Storage timeout
    # blocks the response when the bucket is not yet configured.
    storage_key = f"specimens/{specimen_id}/images/{image_id}.jpg"

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

    # ── 7. Attempt AI inference (no-op until urolens_ai is installed) ──────────
    ai_findings = await _try_run_inference(raw_bytes, result_id, now_iso)

    return AnalysisResultResponse(
        id=result_id,
        result_id=result_id,
        specimen_id=specimen_id,
        image_id=image_id,
        status="PENDING_CONFIRM",
        ai_findings=ai_findings,
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
    medtech_id = uuid.UUID(claims["user_id"])
    result = await _retake_service.discard_and_retake(
        image_id=image_id,
        medtech_id=medtech_id,
        request=request,
    )
    return ImageDiscardResponse(**result)
