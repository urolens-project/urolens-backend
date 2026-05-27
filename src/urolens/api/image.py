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
from app.config import SUPABASE_IMAGE_BUCKET

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/images", tags=["images"])
_retake_service = ImageRetakeService()

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}
MIME_TO_EXT = {"image/jpeg": "jpg", "image/png": "png"}
AI_MODEL_VERSION = "mvp-v1.0"


# ── AI inference hook ────────────────────────────────────────────────────────

async def _try_run_inference(
    raw_bytes: bytes,
    result_id: uuid.UUID,
    now_iso: str,
) -> dict | None:
    """
    Attempt AI inference via the urolens_ai package.

    Returns the findings dict on success, None when the package is absent or
    inference fails. DB persistence is best-effort and never blocks the response.
    """
    try:
        from urolens_ai import infer  # type: ignore[import]
    except ImportError:
        return None

    try:
        # Run synchronous YOLOv8 inference in a thread to avoid blocking the event loop
        result = await asyncio.to_thread(infer, raw_bytes)
        # Normalize class names: model uses dashes (epithelial-cells),
        # config.yaml and smart diagnosis expect underscores (epithelial_cells)
        findings: dict = {k.replace("-", "_"): v for k, v in result.particles.items()}
    except Exception as exc:
        log.warning("AI inference failed for result %s: %s", result_id, exc)
        return None

    # Persist findings back to DB — failure here must not suppress the findings
    try:
        await (
            sb.table("analysis_results")
            .update({"ai_findings": findings})
            .eq("result_id", str(result_id))
            .execute()
        )
    except Exception as exc:
        log.warning("Could not persist ai_findings for result %s: %s", result_id, exc)

    return findings


async def _try_run_smart_diagnosis(
    findings: dict,
    result_id: uuid.UUID,
) -> dict | None:
    """
    Run Smart Diagnosis on ai_findings immediately after upload so the MedTech
    sees both panels on the first load — before they click Confirm.

    Writes only to analysis_results.smart_diagnosis (the JSONB column the mobile
    reads via sync). Does NOT insert into smart_diagnosis_outputs — that formal
    audit record is created by SmartDiagnosisService at confirmation time.

    Best-effort: failure never blocks the upload response.
    """
    try:
        from urolens_ai import generate_smart_diagnosis  # type: ignore[import]
        from ..services.smart_diagnosis_service import _build_evidence_map

        engine_output = generate_smart_diagnosis(findings)
        evidence_map = _build_evidence_map(engine_output)
        smart_diagnosis = {
            **evidence_map,
            "no_significant_indicators": engine_output.no_significant_indicators,
        }

        await (
            sb.table("analysis_results")
            .update({"smart_diagnosis": smart_diagnosis})
            .eq("result_id", str(result_id))
            .execute()
        )
        return smart_diagnosis
    except Exception as exc:
        log.warning("Smart Diagnosis failed at upload for result %s: %s", result_id, exc)
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
    smart_diagnosis: dict | None = None


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

    # ── 4. Upload file to Supabase Storage ────────────────────────────────────
    ext = MIME_TO_EXT[content_type]
    storage_key = f"specimens/{specimen_id}/images/{image_id}.{ext}"
    try:
        await sb.storage.from_(SUPABASE_IMAGE_BUCKET).upload(
            path=storage_key,
            file=raw_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        log.info("Uploaded image to storage: %s/%s", SUPABASE_IMAGE_BUCKET, storage_key)
    except Exception as exc:
        log.warning("Supabase Storage upload failed (bucket '%s'): %s", SUPABASE_IMAGE_BUCKET, exc)

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
        update_resp = await (
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
        if not update_resp.data:
            log.error("analysis_results UPDATE returned no data for result_id=%s", result_id)
    else:
        result_id = uuid.uuid4()
        insert_resp = await (
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
        if not insert_resp.data:
            log.error(
                "analysis_results INSERT returned no data — row likely not created. "
                "specimen_id=%s result_id=%s",
                specimen_id, result_id,
            )
            raise ImageFormatError("Failed to create analysis result record.")

    # ── 7. Attempt AI inference (no-op until urolens_ai is installed) ──────────
    ai_findings = await _try_run_inference(raw_bytes, result_id, now_iso)

    # ── 8. Pre-compute Smart Diagnosis so it shows alongside AI Findings ───────
    # Writes to analysis_results.smart_diagnosis only — smart_diagnosis_outputs
    # is populated later by SmartDiagnosisService at confirmation time.
    smart_diagnosis: dict | None = None
    if ai_findings:
        smart_diagnosis = await _try_run_smart_diagnosis(ai_findings, result_id)

    return AnalysisResultResponse(
        id=result_id,
        result_id=result_id,
        specimen_id=specimen_id,
        image_id=image_id,
        status="PENDING_CONFIRM",
        ai_findings=ai_findings,
        flagged_anomalies=None,
        smart_diagnosis=smart_diagnosis,
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
