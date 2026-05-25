# src/urolens/services/ai_integration_service.py

# To be tested with AI Lead, Harley Reyes
"""
AI Integration Service — T2.7

Wraps urolens_ai.infer() and owns the full upload → inference → persist lifecycle.

Key contracts:
  - All AI exceptions are caught and logged — they never propagate to the route layer.
  - The analysis_result row is always created, even on AI failure (status=FAILED).
  - Audit event IMAGE_UPLOADED is fired on successful upload acceptance.
  - image.status transitions: UPLOADED → PROCESSING → PROCESSED | FAILED
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError
from fastapi import UploadFile
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    ImageResolutionError,
    ImageFormatError,
    StorageError,
    SpecimenNotFoundError,
)
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus
from ..core.config import settings

log = logging.getLogger(__name__)

# Minimum resolution required by AI Engineer's spec
MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}


class AIIntegrationService:
    def __init__(
        self,
        db: AsyncSession,
        audit_logger: AuditLogger,
        s3_client: Any | None = None,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger
        # Allow injection for testing; fall back to real boto3 client
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
        2. Upload to S3.
        3. Create Image row (status=UPLOADED).
        4. Create AnalysisResult row (status=PENDING_REVIEW).
        5. Attempt inference; update statuses accordingly.
        6. Fire IMAGE_UPLOADED audit event.

        Raises
        ------
        ImageFormatError      — unsupported MIME type
        ImageResolutionError  — below 640×480
        StorageError          — S3 upload failed
        SpecimenNotFoundError — specimen_id not in DB
        """
        raw_bytes = await file.read()

        # ── 1. Validate ───────────────────────────────────────────────────
        content_type = file.content_type or ""
        self._validate_format(content_type)
        width, height = self._validate_resolution(raw_bytes)

        # ── 2. Upload to S3 ───────────────────────────────────────────────
        s3_key = self._build_s3_key(specimen_id)
        await self._upload_to_s3(raw_bytes, s3_key, content_type)

        # ── 3. Persist Image row ──────────────────────────────────────────
        image = Image(
            specimen_id=specimen_id,
            uploaded_by=uploader_id,
            s3_key=s3_key,
            s3_bucket=settings.S3_BUCKET,
            original_filename=file.filename,
            mime_type=content_type,
            file_size_bytes=len(raw_bytes),
            width_px=width,
            height_px=height,
            status=ImageStatus.UPLOADED,
        )
        self.db.add(image)
        await self.db.flush()  # get image.id without committing

        # ── 4. Persist AnalysisResult row ─────────────────────────────────
        result = AnalysisResult(
            specimen_id=specimen_id,
            image_id=image.id,
            status=ResultStatus.PENDING_REVIEW,
        )
        self.db.add(result)
        await self.db.flush()

        # ── 5. Run inference (failure-isolated) ───────────────────────────
        await self._run_inference(image, result, raw_bytes)

        # ── 6. Audit ──────────────────────────────────────────────────────
        await self.audit_logger.record(
            event_type="IMAGE_UPLOADED",
            entity_type="image",
            entity_id=image.id,
            user_id=uploader_id,
            detail_json={
                "specimen_id": str(specimen_id),
                "mime_type": content_type,
                "width_px": width,
                "height_px": height,
                "file_size_bytes": len(raw_bytes),
            },
            db=self.db,
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
        Called from image_retake_service — kept here for separation of concerns.
        """
        image = await self.db.get(Image, image_id)
        if image is None:
            raise ValueError(f"Image {image_id} not found")
        if image.is_discarded:
            return image  # idempotent

        image.status = ImageStatus.DISCARDED
        image.discarded_by = discarded_by
        from datetime import datetime, timezone
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
        """Return (width, height); raise ImageResolutionError if below minimum."""
        import io
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
        import io

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._s3.upload_fileobj(
                    io.BytesIO(raw_bytes),
                    settings.S3_BUCKET,
                    s3_key,
                    ExtraArgs={"ContentType": content_type},
                ),
            )
        except BotoCoreError as exc:
            raise StorageError(f"S3 upload failed: {exc}") from exc

    async def _run_inference(
        self, image: Image, result: AnalysisResult, raw_bytes: bytes
    ) -> None:
        """
        Call the AI Engineer's infer() and update the result row.

        ALL exceptions are caught — inference failure must not break the upload
        acceptance response. The result row is set to FAILED and the error is logged.
        """
        try:
            # Lazy import — the AI library is a separately installed package
            from urolens_ai import infer  # type: ignore[import]

            image.status = ImageStatus.PROCESSING
            await self.db.flush()

            inference_result = infer(raw_bytes)
            # InferenceResult is a dataclass; serialise to dict for JSONB storage
            result.ai_findings = inference_result.to_dict()  # type: ignore[attr-defined]
            image.status = ImageStatus.PROCESSED

        except Exception as exc:  # noqa: BLE001
            log.exception(
                "AI inference failed for image %s (specimen %s): %s",
                image.id,
                image.specimen_id,
                exc,
            )
            image.status = ImageStatus.FAILED
            result.status = ResultStatus.FAILED
            result.ai_findings = None