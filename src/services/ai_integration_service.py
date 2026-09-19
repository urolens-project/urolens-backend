"""AI Integration Service — T2.7

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
from typing import Any

from fastapi import UploadFile
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.config import settings
from ..core.exceptions import ImageFormatError, ImageResolutionError
from ..core.supabase import supabase as sb
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

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        self.db = db
        self.auditLogger = auditLogger

    # ── Public API ────────────────────────────────────────────────────────

    async def handleUpload(
        self,
        specimenId: uuid.UUID,
        uploaderId: uuid.UUID,
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
        rawBytes = await file.read()

        contentType = file.content_type or ""
        self._validateFormat(contentType)
        width, height = await self._validateResolution(rawBytes)

        await self._replacePreviousImage(specimenId)

        imageId = uuid.uuid4()
        storageKey = self._buildStorageKey(specimenId, imageId, contentType)
        await self._uploadToStorage(rawBytes, storageKey, contentType)

        image = Image(
            imageId=imageId,
            specimenId=specimenId,
            uploadedBy=uploaderId,
            storageKey=storageKey,
            fileFormat=MIME_TO_FORMAT[contentType],
            widthPx=width,
            heightPx=height,
            fileSizeBytes=len(rawBytes),
            status=ImageStatus.ACTIVE,
        )
        self.db.add(image)
        await self.db.flush([image])

        result = await self._getOrCreateResult(specimenId, image.imageId)

        findings = await self._runInference(result, rawBytes)
        if findings:
            await self._runSmartDiagnosis(result, findings)

        await self.auditLogger.record(
            eventType="IMAGE_UPLOADED",
            entityType="image",
            entityId=image.imageId,
            userId=uploaderId,
            detailJson={
                "specimen_id": str(specimenId),
                "file_format": MIME_TO_FORMAT[contentType],
                "width_px": width,
                "height_px": height,
                "file_size_bytes": len(rawBytes),
            },
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(result)
        return result

    # ── Private helpers ──────────────────────────────────────────────────

    def _validateFormat(self, contentType: str) -> None:
        """Raise ImageFormatError if content_type isn't JPEG or PNG."""
        if contentType not in ALLOWED_MIME_TYPES:
            raise ImageFormatError(
                f"Unsupported image format '{contentType}'. "
                f"Accepted: {', '.join(ALLOWED_MIME_TYPES)}"
            )

    async def _validateResolution(self, rawBytes: bytes) -> tuple[int, int]:
        """Return (width, height); raise ImageResolutionError if below minimum.

        PIL.Image.open is synchronous/blocking — run in a thread so a large
        image doesn't stall the event loop.
        """

        def _readDimensions() -> tuple[int, int]:
            img = PILImage.open(io.BytesIO(rawBytes))
            return img.size

        try:
            width, height = await asyncio.to_thread(_readDimensions)
        except Exception as exc:
            raise ImageFormatError(f"Cannot read image file: {exc}") from exc

        if width < MIN_WIDTH or height < MIN_HEIGHT:
            raise ImageResolutionError(
                f"Image resolution {width}x{height} is below the minimum "
                f"{MIN_WIDTH}x{MIN_HEIGHT} required for AI analysis."
            )
        return width, height

    def _buildStorageKey(
        self, specimenId: uuid.UUID, imageId: uuid.UUID, contentType: str
    ) -> str:
        # Builds the Supabase Storage object path for an uploaded image.
        ext = MIME_TO_EXT[contentType]
        return f"specimens/{specimenId}/images/{imageId}.{ext}"

    async def _uploadToStorage(
        self, rawBytes: bytes, storageKey: str, contentType: str
    ) -> None:
        """Upload to Supabase Storage.

        Failure is logged, not fatal — matches the router's proven
        production behavior: the image row is still written even if the
        bucket write failed, rather than blocking the whole upload response
        on a storage-layer issue.
        """
        try:
            await sb.storage.from_(settings.supabaseImageBucket).upload(
                path=storageKey,
                file=rawBytes,
                file_options={"content-type": contentType, "upsert": "true"},
            )
            log.info("Uploaded image to storage: %s/%s", settings.supabaseImageBucket, storageKey)
        except Exception as exc:
            log.warning(
                "Supabase Storage upload failed (bucket '%s'): %s", settings.supabaseImageBucket, exc
            )

    async def _replacePreviousImage(self, specimenId: uuid.UUID) -> None:
        """Mark the previous ACTIVE image for this specimen, if any, REPLACED."""
        stmt = select(Image).where(
            Image.specimenId == specimenId, Image.status == ImageStatus.ACTIVE
        )
        previous = (await self.db.execute(stmt)).scalar_one_or_none()
        if previous:
            previous.status = ImageStatus.REPLACED
            await self.db.flush([previous])

    async def _getOrCreateResult(
        self, specimenId: uuid.UUID, imageId: uuid.UUID
    ) -> AnalysisResult:
        """Attach the new image to the specimen's AnalysisResult, creating
        one if this is the first image for the specimen.

        Resets `ai_findings`/`flagged_anomalies`/`particle_classes` since a
        new image means the prior findings no longer apply.
        """
        specStmt = select(Specimen).where(Specimen.specimenId == specimenId)
        specimen = (await self.db.execute(specStmt)).scalar_one_or_none()

        patientId = None
        if specimen and specimen.labRequestId:
            lrStmt = select(LabRequest.patientId).where(
                LabRequest.labRequestId == specimen.labRequestId
            )
            patientId = (await self.db.execute(lrStmt)).scalar_one_or_none()

        stmt = select(AnalysisResult).where(AnalysisResult.specimenId == specimenId)
        result = (await self.db.execute(stmt)).scalar_one_or_none()

        if result:
            result.imageId = imageId
            result.status = ResultStatus.PENDING_CONFIRM
            result.aiFindings = {}
            result.flaggedAnomalies = {}
            result.particleClasses = {}
            if patientId and not result.patientId:
                result.patientId = patientId
            await self.db.flush([result])
        else:
            result = AnalysisResult(
                specimenId=specimenId,
                imageId=imageId,
                patientId=patientId,
                status=ResultStatus.PENDING_CONFIRM,
                modelVersion=settings.aiModelVersion,
            )
            self.db.add(result)
            await self.db.flush([result])

        return result

    async def _runInference(
        self, result: AnalysisResult, rawBytes: bytes
    ) -> dict | None:
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
            inferenceResult = await asyncio.to_thread(infer, rawBytes)
            # Model emits dashes (epithelial-cells); config.yaml and Smart
            # Diagnosis expect underscores (epithelial_cells).
            findings: dict = {
                k.replace("-", "_"): v for k, v in inferenceResult.particles.items()
            }
        except Exception as exc:
            log.warning("AI inference failed for result %s: %s", result.resultId, exc)
            return None

        result.aiFindings = findings
        result.flaggedAnomalies = {k: v for k, v in findings.items() if v > 0}
        await self.db.flush([result])
        return findings

    async def _runSmartDiagnosis(
        self, result: AnalysisResult, findings: dict
    ) -> dict | None:
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

            from .smart_diagnosis_service import _buildEvidenceMap

            engineOutput = generate_smart_diagnosis(findings)
            evidenceMap = _buildEvidenceMap(engineOutput)
            smartDiagnosis = {
                **evidenceMap,
                "no_significant_indicators": engineOutput.no_significant_indicators,
            }
            result.smartDiagnosis = smartDiagnosis
            await self.db.flush([result])
            return smartDiagnosis
        except Exception as exc:
            log.warning(
                "Smart Diagnosis failed at upload for result %s: %s", result.resultId, exc
            )
            return None
