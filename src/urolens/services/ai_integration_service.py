from __future__ import annotations

# To be tested with AI Lead, Harley Reyes
"""
AI Integration Service — T2.7

Wraps urolens_ai.infer() and owns the full upload → inference → persist lifecycle.

Key contracts:
  - All AI exceptions are caught and logged — they never propagate to the route layer.
  - The analysis_result row is always created, even on AI failure (status=FAILED).
  - Audit event IMAGE_UPLOADED is fired on successful upload acceptance.
  - image.status: ACTIVE while in use, REPLACED on retake, DISCARDED on explicit discard.
"""

import io
import logging
import uuid
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError
from fastapi import UploadFile
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.config import AI_MODEL_VERSION, S3_BUCKET
from ..core.exceptions import ImageFormatError, ImageResolutionError, StorageError
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus

log = logging.getLogger(__name__)

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}


class AIIntegrationService:
    def __init__(
        self,
        db: AsyncSession,
        audit_logger: AuditLogger,
        s3_client: Any | None = None,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger
        self._s3 = s3_client or boto3.client("s3")

    # ── Public API ────────────────────────────────────────────────────────────

    async def handle_upload(
        self,
        specimen_id: uuid.UUID,
        uploader_id: uuid.UUID,
        file: UploadFile,
        request: Any,
    ) -> AnalysisResult:
        """
        Full upload → validate → store → infer pipeline.

        Steps
        -----
        1. Read file bytes and validate format + resolution.
        2. Replace any ACTIVE image for this specimen (mark REPLACED).
        3. Upload to S3.
        4. Create Image row (status=ACTIVE).
        5. Create or update AnalysisResult row (status=PENDING_CONFIRM).
        6. Attempt inference; on failure result.status stays PENDING_CONFIRM with
           empty ai_findings so the MedTech can retake.
        7. Fire IMAGE_UPLOADED audit event.

        Raises
        ------
        ImageFormatError      — unsupported MIME type
        ImageResolutionError  — below 640×480
        StorageError          — S3 upload failed
        """
        raw_bytes = await file.read()

        content_type = file.content_type or ""
        self._validate_format(content_type)
        width, height = self._validate_resolution(raw_bytes)

        # Mark any previous ACTIVE image for this specimen as REPLACED
        await self._replace_previous_image(specimen_id)

        s3_key = self._build_s3_key(specimen_id)
        await self._upload_to_s3(raw_bytes, s3_key, content_type)

        image = Image(
            specimen_id=specimen_id,
            uploaded_by=uploader_id,
            storage_key=s3_key,
            file_format=MIME_TO_FORMAT[content_type],
            width_px=width,
            height_px=height,
            file_size_bytes=len(raw_bytes),
            status=ImageStatus.ACTIVE,
        )
        self.db.add(image)
        await self.db.flush()

        result = await self._get_or_create_result(specimen_id, image.image_id)

        await self._run_inference(result, raw_bytes)

        await self.audit_logger.record(
            event_type="IMAGE_UPLOADED",
            entity_type="image",
            entity_id=image.image_id,
            user_id=uploader_id,
            detail_json={
                "specimen_id": str(specimen_id),
                "file_format": MIME_TO_FORMAT[content_type],
                "width_px": width,
                "height_px": height,
                "file_size_bytes": len(raw_bytes),
            },
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(result)
        return result

    async def discard_image(
        self,
        image_id: uuid.UUID,
        discarded_by: uuid.UUID,
        request: Any,
    ) -> Image:
        """
        Mark an image as DISCARDED so the MedTech can retake.
        Called from ImageRetakeService.
        """
        image = await self.db.get(Image, image_id)
        if image is None:
            raise ValueError(f"Image {image_id} not found")
        if image.is_discarded:
            return image  # idempotent

        from datetime import datetime, timezone
        image.status = ImageStatus.DISCARDED
        image.discarded_at = datetime.now(timezone.utc)
        await self.db.flush()
        return image

    # ── Private helpers ───────────────────────────────────────────────────────

    def _validate_format(self, content_type: str) -> None:
        if content_type not in ALLOWED_MIME_TYPES:
            raise ImageFormatError(
                f"Unsupported image format '{content_type}'. "
                f"Accepted: {', '.join(ALLOWED_MIME_TYPES)}"
            )

    def _validate_resolution(self, raw_bytes: bytes) -> tuple[int, int]:
        try:
            img = PILImage.open(io.BytesIO(raw_bytes))
            width, height = img.size
        except Exception as exc:
            raise ImageFormatError(f"Cannot read image file: {exc}") from exc

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise ImageResolutionError(
                f"Image resolution {width}×{height} is below the minimum "
                f"{MIN_WIDTH}×{MIN_HEIGHT} required for AI analysis."
            )
        return width, height

    def _build_s3_key(self, specimen_id: uuid.UUID) -> str:
        return f"specimens/{specimen_id}/images/{uuid.uuid4()}.jpg"

    async def _upload_to_s3(
        self, raw_bytes: bytes, s3_key: str, content_type: str
    ) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._s3.upload_fileobj(
                    io.BytesIO(raw_bytes),
                    S3_BUCKET,
                    s3_key,
                    ExtraArgs={"ContentType": content_type},
                ),
            )
        except BotoCoreError as exc:
            raise StorageError(f"S3 upload failed: {exc}") from exc

    async def _replace_previous_image(self, specimen_id: uuid.UUID) -> None:
        """Mark previous ACTIVE image for the specimen as REPLACED."""
        from sqlalchemy import select
        stmt = select(Image).where(
            Image.specimen_id == specimen_id,
            Image.status == ImageStatus.ACTIVE,
        )
        result = await self.db.execute(stmt)
        previous = result.scalar_one_or_none()
        if previous:
            previous.status = ImageStatus.REPLACED
            await self.db.flush()

    async def _get_or_create_result(
    self, specimen_id: uuid.UUID, image_id: uuid.UUID
    ) -> AnalysisResult:
        from sqlalchemy import select
        from app.db.supabase import supabase

        # Look up patient_id via specimen → lab_request (Supabase)
        spec_stmt = select(Specimen).where(Specimen.specimen_id == specimen_id)
        spec_row = await self.db.execute(spec_stmt)
        specimen = spec_row.scalar_one_or_none()

        patient_id = None
        if specimen and specimen.lab_request_id:
            try:
                lr_res = await supabase.table("lab_requests").select("patient_id").eq(
                    "lab_request_id", str(specimen.lab_request_id)
                ).limit(1).execute()
                if lr_res.data:
                    patient_id = lr_res.data[0].get("patient_id")
            except Exception:
                pass  # best-effort — don't break upload if lookup fails

        stmt = select(AnalysisResult).where(AnalysisResult.specimen_id == specimen_id)
        db_result = await self.db.execute(stmt)
        result = db_result.scalar_one_or_none()

        if result:
            result.image_id = image_id
            result.status = ResultStatus.PENDING_CONFIRM
            result.ai_findings = {}
            result.flagged_anomalies = {}
            result.particle_classes = {}
            if patient_id and not result.patient_id:
                result.patient_id = patient_id
            await self.db.flush()
        else:
            result = AnalysisResult(
                specimen_id=specimen_id,
                image_id=image_id,
                patient_id=patient_id,
                status=ResultStatus.PENDING_CONFIRM,
                model_version=AI_MODEL_VERSION,
            )
            self.db.add(result)
            await self.db.flush()

        return result

    async def _run_inference(
        self, result: AnalysisResult, raw_bytes: bytes
    ) -> None:
        """
        Call the AI Engineer's infer() and update the result row.
        ALL exceptions are caught — inference failure must not break the upload.
        """
        try:
            from urolens_ai import infer  # type: ignore[import]

            inference_result = infer(raw_bytes)
            findings: dict = inference_result.particles

            result.ai_findings = findings
            result.flagged_anomalies = {
                k: v for k, v in findings.items() if v > 0
            }

        except Exception as exc:
            log.exception(
                "AI inference failed for specimen %s: %s",
                result.specimen_id,
                exc,
            )
            result.ai_findings = {}
            result.flagged_anomalies = {}

        await self.db.flush()
