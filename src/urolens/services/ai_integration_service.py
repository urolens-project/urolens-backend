"""
AI Integration Service — T2.7

Wraps urolens_ai.infer() and owns the full upload -> inference -> persist
lifecycle. Canonical implementation for the image/AI analysis domain
(consolidation plan rows 4-5, rule 14 - one implementation per feature).

Two other implementations existed before this merge and are now gone:
`ai_integration.py` (deleted - targeted a schema that no longer exists,
`ImageStatus.UPLOADED`/`ResultStatus.PENDING_REVIEW` aren't real enum members
here, and imported a `settings` object `core/config.py` never defined) and
~250 lines of inline logic in the router (`src/urolens/api/image.py`) that
this class now owns instead.

Ported from the router's proven inline implementation, since that was the
one actually exercised in production, despite living in the wrong layer:
  - Storage is Supabase Storage, not S3/boto3 - confirmed no AWS credentials
    exist anywhere in this project (.env.example, requirements.txt).
  - Storage upload failure is logged and non-fatal - the image row is still
    written even if the bucket write failed. Kept exactly as the router had
    it; not revisited as part of this merge.
  - AI inference failure is caught and logged; the result stays
    PENDING_CONFIRM with empty findings so the MedTech can retake, rather
    than a hard FAILED state.
  - Particle class names are normalized dash -> underscore (the model emits
    `epithelial-cells`; config.yaml and Smart Diagnosis expect
    `epithelial_cells`).
  - Smart Diagnosis is pre-computed at upload time (writes only
    `analysis_results.smart_diagnosis`) so both panels are visible before
    the MedTech clicks Confirm - the formal `smart_diagnosis_outputs` audit
    record is still created separately by SmartDiagnosisService at
    confirmation time.
"""
from __future__ import annotations

import asyncio
import io
import logging
import uuid
from typing import Any, Optional

from fastapi import UploadFile
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.config import settings
from ..core.supabase import supabase as sb
from ..core.exceptions import ImageFormatError, ImageResolutionError
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus
from ..models.lab_request import LabRequest
from ..models.specimen import Specimen

log = logging.getLogger(__name__)

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}
MIME_TO_EXT = {"image/jpeg": "jpg", "image/png": "png"}


class AIIntegrationService:
    """Owns microscopy image upload, AI inference, and Smart Diagnosis precompute.

    Discard/retake is a separate class, `ImageRetakeService` — not merged
    into this one, per the plan's row 5 decision to keep it independent.
    """

    def __init__(self, db: AsyncSession, audit_logger: AuditLogger) -> None:
        self.db = db
        self.audit_logger = audit_logger

    # ── Public API ────────────────────────────────────────────────────────

    async def handle_upload(
        self,
        specimen_id: uuid.UUID,
        uploader_id: uuid.UUID,
        file: UploadFile,
        request: Any,
    ) -> AnalysisResult:
        """Validate, store, and run AI inference on an uploaded microscopy image.

        Args:
            specimen_id: Specimen this image belongs to.
            uploader_id: user_id of the authenticated MedTech uploading it.
            file: Multipart upload — JPEG or PNG, minimum 640x480.
            request: Inbound request, forwarded to the audit logger for IP
                attribution.

        Returns:
            The specimen's AnalysisResult row (created on first upload,
            reset and reattached on retake), with `ai_findings`/
            `smart_diagnosis` populated if inference succeeded.

        Raises:
            ImageFormatError: unsupported MIME type or unreadable file.
            ImageResolutionError: below the 640x480 minimum.
        """
        raw_bytes = await file.read()

        content_type = file.content_type or ""
        self._validate_format(content_type)
        width, height = await self._validate_resolution(raw_bytes)

        await self._replace_previous_image(specimen_id)

        image_id = uuid.uuid4()
        storage_key = self._build_storage_key(specimen_id, image_id, content_type)
        await self._upload_to_storage(raw_bytes, storage_key, content_type)

        image = Image(
            image_id=image_id,
            specimen_id=specimen_id,
            uploaded_by=uploader_id,
            storage_key=storage_key,
            file_format=MIME_TO_FORMAT[content_type],
            width_px=width,
            height_px=height,
            file_size_bytes=len(raw_bytes),
            status=ImageStatus.ACTIVE,
        )
        self.db.add(image)
        await self.db.flush([image])

        result = await self._get_or_create_result(specimen_id, image.image_id)

        findings = await self._run_inference(result, raw_bytes)
        if findings:
            await self._run_smart_diagnosis(result, findings)

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

    # ── Private helpers ──────────────────────────────────────────────────

    def _validate_format(self, content_type: str) -> None:
        """Raise ImageFormatError if content_type isn't JPEG or PNG."""
        if content_type not in ALLOWED_MIME_TYPES:
            raise ImageFormatError(
                f"Unsupported image format '{content_type}'. "
                f"Accepted: {', '.join(ALLOWED_MIME_TYPES)}"
            )

    async def _validate_resolution(self, raw_bytes: bytes) -> tuple[int, int]:
        """Return (width, height); raise ImageResolutionError if below minimum.

        PIL.Image.open is synchronous/blocking — run in a thread so a large
        image doesn't stall the event loop.
        """

        def _read_dimensions() -> tuple[int, int]:
            img = PILImage.open(io.BytesIO(raw_bytes))
            return img.size

        try:
            width, height = await asyncio.to_thread(_read_dimensions)
        except Exception as exc:
            raise ImageFormatError(f"Cannot read image file: {exc}") from exc

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise ImageResolutionError(
                f"Image resolution {width}x{height} is below the minimum "
                f"{MIN_WIDTH}x{MIN_HEIGHT} required for AI analysis."
            )
        return width, height

    def _build_storage_key(
        self, specimen_id: uuid.UUID, image_id: uuid.UUID, content_type: str
    ) -> str:
        ext = MIME_TO_EXT[content_type]
        return f"specimens/{specimen_id}/images/{image_id}.{ext}"

    async def _upload_to_storage(
        self, raw_bytes: bytes, storage_key: str, content_type: str
    ) -> None:
        """Upload to Supabase Storage.

        Failure is logged, not fatal — matches the router's proven
        production behavior: the image row is still written even if the
        bucket write failed, rather than blocking the whole upload response
        on a storage-layer issue.
        """
        try:
            await sb.storage.from_(settings.supabase_image_bucket).upload(
                path=storage_key,
                file=raw_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            log.info("Uploaded image to storage: %s/%s", settings.supabase_image_bucket, storage_key)
        except Exception as exc:
            log.warning(
                "Supabase Storage upload failed (bucket '%s'): %s", settings.supabase_image_bucket, exc
            )

    async def _replace_previous_image(self, specimen_id: uuid.UUID) -> None:
        """Mark the previous ACTIVE image for this specimen, if any, REPLACED."""
        stmt = select(Image).where(
            Image.specimen_id == specimen_id, Image.status == ImageStatus.ACTIVE
        )
        previous = (await self.db.execute(stmt)).scalar_one_or_none()
        if previous:
            previous.status = ImageStatus.REPLACED
            await self.db.flush([previous])

    async def _get_or_create_result(
        self, specimen_id: uuid.UUID, image_id: uuid.UUID
    ) -> AnalysisResult:
        """Attach the new image to the specimen's AnalysisResult, creating
        one if this is the first image for the specimen.

        Resets `ai_findings`/`flagged_anomalies`/`particle_classes` since a
        new image means the prior findings no longer apply.
        """
        spec_stmt = select(Specimen).where(Specimen.specimen_id == specimen_id)
        specimen = (await self.db.execute(spec_stmt)).scalar_one_or_none()

        patient_id = None
        if specimen and specimen.lab_request_id:
            lr_stmt = select(LabRequest.patient_id).where(
                LabRequest.lab_request_id == specimen.lab_request_id
            )
            patient_id = (await self.db.execute(lr_stmt)).scalar_one_or_none()

        stmt = select(AnalysisResult).where(AnalysisResult.specimen_id == specimen_id)
        result = (await self.db.execute(stmt)).scalar_one_or_none()

        if result:
            result.image_id = image_id
            result.status = ResultStatus.PENDING_CONFIRM
            result.ai_findings = {}
            result.flagged_anomalies = {}
            result.particle_classes = {}
            if patient_id and not result.patient_id:
                result.patient_id = patient_id
            await self.db.flush([result])
        else:
            result = AnalysisResult(
                specimen_id=specimen_id,
                image_id=image_id,
                patient_id=patient_id,
                status=ResultStatus.PENDING_CONFIRM,
                model_version=settings.ai_model_version,
            )
            self.db.add(result)
            await self.db.flush([result])

        return result

    async def _run_inference(
        self, result: AnalysisResult, raw_bytes: bytes
    ) -> Optional[dict]:
        """Run YOLOv8 inference and persist findings onto the result row.

        All exceptions are caught — inference failure must never block the
        upload response, and `result.status` stays `PENDING_CONFIRM` either
        way so the MedTech can retake if findings come back empty.

        Returns:
            The findings dict (particle class -> count) on success, or None
            if the `urolens_ai` package is absent or inference failed.
        """
        try:
            from urolens_ai import infer  # type: ignore[import]
        except ImportError:
            return None

        try:
            inference_result = await asyncio.to_thread(infer, raw_bytes)
            # Model emits dashes (epithelial-cells); config.yaml and Smart
            # Diagnosis expect underscores (epithelial_cells).
            findings: dict = {
                k.replace("-", "_"): v for k, v in inference_result.particles.items()
            }
        except Exception as exc:
            log.warning("AI inference failed for result %s: %s", result.result_id, exc)
            return None

        result.ai_findings = findings
        result.flagged_anomalies = {k: v for k, v in findings.items() if v > 0}
        await self.db.flush([result])
        return findings

    async def _run_smart_diagnosis(
        self, result: AnalysisResult, findings: dict
    ) -> Optional[dict]:
        """Pre-compute Smart Diagnosis at upload time so both panels are
        visible before the MedTech clicks Confirm.

        Writes only `analysis_results.smart_diagnosis` (the JSONB column the
        mobile app reads via sync) — does NOT insert into
        `smart_diagnosis_outputs`, which `SmartDiagnosisService` still
        creates as the formal audit record at confirmation time. Best
        effort: failure never blocks the upload response.
        """
        try:
            from urolens_ai import generate_smart_diagnosis  # type: ignore[import]

            from .smart_diagnosis_service import _build_evidence_map

            engine_output = generate_smart_diagnosis(findings)
            evidence_map = _build_evidence_map(engine_output)
            smart_diagnosis = {
                **evidence_map,
                "no_significant_indicators": engine_output.no_significant_indicators,
            }
            result.smart_diagnosis = smart_diagnosis
            await self.db.flush([result])
            return smart_diagnosis
        except Exception as exc:
            log.warning(
                "Smart Diagnosis failed at upload for result %s: %s", result.result_id, exc
            )
            return None
