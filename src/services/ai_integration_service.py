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
  - Nothing is saved unless the image is both analyzed and stored
    (UROLENS-230): an AI failure is `AI_ANALYSIS_FAILED`, a storage failure
    `STORAGE_ERROR` (both 503), so the app keeps the image for a retry.
    Before, both were logged and the upload "succeeded" — with empty
    findings that looked like "no particles", or an image row pointing at a
    file that was never stored.
  - Metadata (EXIF location/device, PNG text) is stripped on the server too,
    whatever the client did, before the image is stored or analyzed.
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
from dataclasses import dataclass
from math import isfinite
from typing import Any

from fastapi import Request, UploadFile
from PIL import Image as PILImage
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.config import settings
from ..core.exceptions import (
    AIAnalysisError,
    ConflictException,
    ImageFormatError,
    ImageResolutionError,
    ImageTooLargeError,
    StorageError,
    UnprocessableException,
)
from ..core.supabase import supabase as sb
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus
from ..models.lab_request import LabRequest
from ..models.manual_override import ManualOverride
from ..models.result_review import ResultReview
from ..models.smart_diagnosis_output import SmartDiagnosisOutput
from ..models.specimen import Specimen
from ..schemas.result_review import AIDetectionItem
from . import specimen_service
from .consent_check import requireProcessingConsent
from .specimen_access import (
    MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES,
    getAssignedSpecimen,
)

log = logging.getLogger(__name__)

MIN_WIDTH = 640
MIN_HEIGHT = 480
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}
MIME_TO_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG"}
MIME_TO_EXT = {"image/jpeg": "jpg", "image/png": "png"}
# 10 MiB. Covers a full-resolution in-app camera JPEG (~3-6 MB) with room to
# spare; the AI model downsizes to ~640 px anyway, so bigger buys nothing.
# Migration 0043 sets the same limit on the storage bucket — keep them equal.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass
class InferenceOutput:
    """Count and spatial output kept together through image persistence."""

    findings: dict[str, int]
    detections: list[dict[str, Any]] | None = None
    modelVersion: str | None = None


def _normalizeDetections(inferenceResult: Any) -> list[dict[str, Any]] | None:
    """Convert accepted pixel xyxy detections to image-relative percentages."""
    detections = getattr(inferenceResult, "detections", None)
    if detections is None:
        return None
    if not isinstance(detections, list):
        raise ValueError("AI detections must be a list")
    if not detections:
        return []
    width, height = inferenceResult.image_width, inferenceResult.image_height
    if not all(isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("AI detection image dimensions must be positive and finite")
    normalized = []
    for detection in detections:
        x1, y1, x2, y2 = detection.bbox
        if not all(isfinite(value) for value in (x1, y1, x2, y2)):
            raise ValueError("AI detection coordinates must be finite")
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("AI detection must cover part of the image")
        box = AIDetectionItem(
            id=str(uuid.uuid4()),
            particleType=detection.class_name.replace("-", "_"),
            confidence=detection.confidence,
            x=100 * x1 / width,
            y=100 * y1 / height,
            w=100 * (x2 - x1) / width,
            h=100 * (y2 - y1) / height,
        )
        normalized.append(box.model_dump())
    return normalized


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

        Nothing is saved unless the image is analyzed and stored. A retake
        resets everything derived from the old image (findings, overrides,
        Smart Diagnosis) but keeps a returned result RETURNED_FOR_CORRECTION,
        so the MedTech still sees the supervisor's reason. A specimen not yet
        started moves to PROCESSING, as "Begin Analysis" would.

        Returns:
            The specimen's AnalysisResult row (created on first upload,
            reset and reattached on retake), with `ai_findings` set and
            `smart_diagnosis` set unless the diagnosis engine failed.

        Raises:
            ImageFormatError: `INVALID_IMAGE_FORMAT`, if the declared type
                isn't JPEG/PNG, or the bytes aren't a readable image of that
                declared type.
            ImageTooLargeError: `IMAGE_TOO_LARGE`, if over `MAX_IMAGE_BYTES`.
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if `specimenId`
                doesn't exist.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen isn't
                assigned to `uploaderId`.
            ConflictException: `SPECIMEN_REJECTED`, if the specimen was
                rejected; `RESULT_NOT_EDITABLE`, if its result has already
                been submitted, approved or released; `CONSENT_REFUSED`, if
                the patient refused consent to processing.
            ImageResolutionError: `INVALID_IMAGE_RESOLUTION`, if below 640x480.
            UnprocessableException: the engine's own code (`NOT_MICROSCOPY` or
                `IMAGE_EXPOSURE`), if the AI engine's input gate rejected the
                image itself before analysis ran.
            AIAnalysisError: `AI_ANALYSIS_FAILED` (503), if AI inference failed.
            StorageError: `STORAGE_ERROR` (503), if the image couldn't be stored.
        """
        contentType = file.content_type or ""
        self._validateFormat(contentType)
        rawBytes = await self._readWithinLimit(file)
        # Access checks run before the file is decoded or stored, so a
        # rejected upload never reaches Pillow or the bucket.
        specimen = await self._requireUploadAllowed(specimenId, uploaderId, request)
        width, height = await self._validateImage(rawBytes, contentType)
        imageBytes = await self._stripMetadata(rawBytes, contentType)
        # Analyze before storing, so a failed analysis leaves nothing behind.
        inference = await self._infer(specimenId, imageBytes)
        findings = inference.findings

        await self._replacePreviousImage(specimenId)

        imageId = uuid.uuid4()
        storageKey = self._buildStorageKey(specimenId, imageId, contentType)
        await self._uploadToStorage(imageBytes, storageKey, contentType)

        image = Image(
            imageId=imageId,
            specimenId=specimenId,
            uploadedBy=uploaderId,
            storageKey=storageKey,
            fileFormat=MIME_TO_FORMAT[contentType],
            widthPx=width,
            heightPx=height,
            fileSizeBytes=len(imageBytes),
            status=ImageStatus.ACTIVE,
        )
        self.db.add(image)
        await self.db.flush([image])

        result = await self._getOrCreateResult(specimen, image.imageId)
        clearedOverrides = await self._clearManualOverrides(result.resultId)

        result.aiFindings = findings
        result.aiDetections = inference.detections
        if inference.modelVersion:
            result.modelVersion = inference.modelVersion
        result.flaggedAnomalies = {k: v for k, v in findings.items() if v > 0}
        await self.db.flush([result])
        if findings:
            await self._runSmartDiagnosis(result, findings)
        startedAnalysis = await specimen_service.markProcessing(self.db, specimen, uploaderId, request)

        await self.auditLogger.record(
            db=self.db,
            eventType="IMAGE_UPLOADED",
            entityType="image",
            entityId=image.imageId,
            userId=uploaderId,
            detailJson={
                "specimen_id": str(specimenId),
                "file_format": MIME_TO_FORMAT[contentType],
                "width_px": width,
                "height_px": height,
                "file_size_bytes": len(imageBytes),
                "manual_overrides_cleared": clearedOverrides,
                "started_analysis": startedAnalysis,
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

    async def _readWithinLimit(self, file: UploadFile) -> bytes:
        """Read the upload, refusing anything over `MAX_IMAGE_BYTES`.

        Never loads more than one byte past the limit into memory.
        """
        if file.size is not None and file.size > MAX_IMAGE_BYTES:
            raise ImageTooLargeError()
        rawBytes = await file.read(MAX_IMAGE_BYTES + 1)
        if len(rawBytes) > MAX_IMAGE_BYTES:
            raise ImageTooLargeError()
        return rawBytes

    async def _requireUploadAllowed(
        self, specimenId: uuid.UUID, uploaderId: uuid.UUID, request: Request | None = None
    ) -> Specimen:
        """Allow the upload only for the assigned MedTech on a live specimen.

        Also refused once the result has been submitted: an upload resets the
        result's findings.

        Returns:
            The specimen, locked until the upload's transaction ends.
        """
        specimen = await getAssignedSpecimen(self.db, specimenId, uploaderId)
        if specimen.status == "REJECTED":
            raise ConflictException(
                code="SPECIMEN_REJECTED",
                message="This specimen was rejected, so no image can be added to it.",
            )
        result = (
            await self.db.execute(
                select(AnalysisResult).where(AnalysisResult.specimenId == specimenId)
            )
        ).scalar_one_or_none()
        if result is not None and result.status not in MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES:
            raise ConflictException(
                code="RESULT_NOT_EDITABLE",
                message="This result has already been submitted, so its image can't be replaced.",
            )
        await requireProcessingConsent(
            self.db, specimen, uploaderId, self.auditLogger, action="IMAGE_UPLOAD", request=request
        )
        return specimen

    async def _validateImage(self, rawBytes: bytes, contentType: str) -> tuple[int, int]:
        """Return (width, height) of a genuine JPEG/PNG matching `contentType`.

        Pillow is limited to the JPEG and PNG decoders: without `formats=`
        it sniffs the bytes and runs whichever parser matches (EPS, GD,
        JPEG2000, ...), whatever `Content-Type` claimed (security audit
        F-05). Synchronous — runs in a thread so it can't stall the loop.
        """

        def _readHeader() -> tuple[str | None, tuple[int, int]]:
            img = PILImage.open(io.BytesIO(rawBytes), formats=list(MIME_TO_FORMAT.values()))
            return img.format, img.size

        try:
            actualFormat, (width, height) = await asyncio.to_thread(_readHeader)
        except Exception as exc:
            log.info("Rejected unreadable image upload", exc_info=True)
            raise ImageFormatError(
                "The file isn't a readable JPEG or PNG image."
            ) from exc
        if actualFormat != MIME_TO_FORMAT[contentType]:
            raise ImageFormatError(
                f"The file content doesn't match its declared type ({contentType})."
            )

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

    async def _stripMetadata(self, rawBytes: bytes, contentType: str) -> bytes:
        """Return the image re-encoded without metadata (EXIF, PNG text chunks).

        The app strips location and device details before uploading, but the
        server can't rely on that (an old app version, a direct API call), and
        a MedTech's phone location has no place in a patient's record
        (RA 10173). JPEGs keep their original quantization (`quality="keep"`),
        so the pixels don't lose quality; the colour profile is kept. Runs in a
        thread — decoding is CPU-bound.

        Raises:
            ImageFormatError: `INVALID_IMAGE_FORMAT`, if the image can't be decoded.
            ImageTooLargeError: `IMAGE_TOO_LARGE`, if re-encoding pushed it over
                `MAX_IMAGE_BYTES`.
        """
        fmt = MIME_TO_FORMAT[contentType]

        def _reencode() -> bytes:
            with PILImage.open(io.BytesIO(rawBytes), formats=[fmt]) as img:
                img.load()
                options: dict[str, Any] = {"icc_profile": img.info.get("icc_profile")}
                if fmt == "JPEG":
                    options.update(quality="keep", subsampling="keep")
                out = io.BytesIO()
                img.save(out, format=fmt, **options)
                return out.getvalue()

        try:
            cleaned = await asyncio.to_thread(_reencode)
        except Exception as exc:
            log.info("Rejected an image that couldn't be re-encoded", exc_info=True)
            raise ImageFormatError("The file isn't a readable JPEG or PNG image.") from exc
        if len(cleaned) > MAX_IMAGE_BYTES:
            raise ImageTooLargeError()
        return cleaned

    async def _uploadToStorage(
        self, rawBytes: bytes, storageKey: str, contentType: str
    ) -> None:
        """Upload to Supabase Storage.

        Raises:
            StorageError: `STORAGE_ERROR`, if the bucket write failed. Nothing
                is saved: an image row pointing at a file that was never
                stored would break the supervisor's image link for good.
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
            raise StorageError() from exc

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
        self, specimen: Specimen, imageId: uuid.UUID
    ) -> AnalysisResult:
        """Attach the new image to the specimen's AnalysisResult, creating
        one if this is the first image for the specimen.

        On a retake, everything derived from the old image is reset — the
        findings, `particle_classes`, the Smart Diagnosis (column, flag and
        output row) and the model version — so nothing from the previous
        image can show against the new one. A result the supervisor returned
        stays RETURNED_FOR_CORRECTION: the MedTech still needs the reason, and
        confirming it is a resubmission (UROLENS-230). Anything else becomes
        PENDING_CONFIRM.
        """
        specimenId = specimen.specimenId
        patientId = None
        if specimen.labRequestId:
            lrStmt = select(LabRequest.patientId).where(
                LabRequest.labRequestId == specimen.labRequestId
            )
            patientId = (await self.db.execute(lrStmt)).scalar_one_or_none()

        stmt = select(AnalysisResult).where(AnalysisResult.specimenId == specimenId)
        result = (await self.db.execute(stmt)).scalar_one_or_none()

        if result:
            result.imageId = imageId
            if result.status != ResultStatus.RETURNED_FOR_CORRECTION:
                result.status = ResultStatus.PENDING_CONFIRM
            result.aiFindings = {}
            result.aiDetections = None
            result.flaggedAnomalies = {}
            result.particleClasses = {}
            result.smartDiagnosis = None
            result.smartDiagnosisUnavailable = False
            result.modelVersion = settings.aiModelVersion
            if patientId and not result.patientId:
                result.patientId = patientId
            # Confirmation writes a fresh one for the new findings.
            await self.db.execute(
                delete(SmartDiagnosisOutput).where(SmartDiagnosisOutput.resultId == result.resultId)
            )
            # Keep review notes, but geometry from the replaced image must not
            # appear over its replacement after the next detail fetch.
            await self.db.execute(
                update(ResultReview)
                .where(ResultReview.resultId == result.resultId)
                .values(spatialAnnotations=None)
            )
            await self.db.flush([result])
        else:
            result = AnalysisResult(
                specimenId=specimenId,
                imageId=imageId,
                patientId=patientId,
                status=ResultStatus.PENDING_CONFIRM,
                modelVersion=settings.aiModelVersion,
                smartDiagnosisUnavailable=False,
            )
            self.db.add(result)
            await self.db.flush([result])

        return result

    async def _clearManualOverrides(self, resultId: uuid.UUID) -> int:
        """Delete the result's manual overrides; return how many were removed.

        A new image replaces the findings those overrides corrected (UROLENS-227,
        UROLENS-228), and confirmation merges every override into
        `particle_classes` — so a kept override would silently correct the new
        analysis with a count taken from the old image. The mobile app warns
        the MedTech before a retake and clears its own copy. The deleted
        values stay in the audit log (`RESULT_OVERRIDDEN` rows).
        """
        deleted = await self.db.execute(delete(ManualOverride).where(ManualOverride.resultId == resultId))
        return deleted.rowcount or 0

    async def _infer(self, specimenId: uuid.UUID, imageBytes: bytes) -> InferenceOutput:
        """Keep particle counts and individual boxes from the same inference run.

        An empty dict is a real result (nothing detected). A failure — the
        engine erroring, its weights missing, the package absent — raises, so
        a failed analysis can never pass for "no particles" (UROLENS-230).

        Raises:
            UnprocessableException: the engine's own code (`NOT_MICROSCOPY` or
                `IMAGE_EXPOSURE`), if its input gate rejected the image before
                analysis ran — the image itself is the problem, not the engine.
            AIAnalysisError: `AI_ANALYSIS_FAILED`, if inference didn't run.
        """
        try:
            from urolens_ai import infer  # type: ignore[import]
            from urolens_ai.utils.exceptions import (
                ImageValidationError,  # type: ignore[import]
            )

            try:
                inferenceResult = await asyncio.to_thread(infer, imageBytes)
            except ImageValidationError as exc:
                # The gate rejected the image itself (not microscopy, or too
                # dark/bright) — a client-correctable 422, not an engine failure.
                log.info(
                    "Image rejected by the input gate for specimen %s: [%s] %s",
                    specimenId, exc.code, exc.message,
                )
                raise UnprocessableException(code=exc.code, message=exc.message) from exc
            # Model emits dashes (epithelial-cells); config.yaml and Smart
            # Diagnosis expect underscores (epithelial_cells).
            return InferenceOutput(
                findings={k.replace("-", "_"): v for k, v in inferenceResult.particles.items()},
                detections=_normalizeDetections(inferenceResult),
                modelVersion=inferenceResult.model_version,
            )
        except UnprocessableException:
            raise
        except Exception as exc:
            log.warning("AI inference failed for specimen %s: %s", specimenId, exc, exc_info=True)
            raise AIAnalysisError() from exc

    async def _runSmartDiagnosis(
        self, result: AnalysisResult, findings: dict
    ) -> dict | None:
        """Pre-compute Smart Diagnosis at upload time so both panels are
        visible before the MedTech clicks Confirm.

        Writes only `analysis_results.smart_diagnosis` (the JSONB column the
        mobile app reads via sync) — does NOT insert into
        `smart_diagnosis_outputs`, which `SmartDiagnosisService` still
        creates as the formal audit record at confirmation time. Best
        effort: failure never blocks the upload response, but marks the
        diagnosis unavailable so the app can say so (UROLENS-146);
        confirmation runs it again and clears the flag if it succeeds.
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
            result.smartDiagnosisUnavailable = True
            return None
