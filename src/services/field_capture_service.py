"""Multi-field capture: an image upload adds one field to the specimen's result.

Used by `POST /api/v1/fields` while `FIELD_WORKFLOW_ENABLED` is on (SDD Module 2
multi-field proposal, transactions 2.3 and 2.4); the single-image
`POST /api/v1/images/upload` answers 410 then. It reuses the
single-image pipeline of `AIIntegrationService` (type and size checks, decode,
metadata strip, storage) and changes only what is persisted: a new `images` row
and a new `result_fields` row, never a replacement. Earlier fields, their images
and any corrections are left alone.
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any, NamedTuple

from fastapi import Request, UploadFile
from sqlalchemy import func, select

from ..core.config import settings
from ..core.exceptions import AIAnalysisError, ConflictException
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.image import Image, ImageStatus
from ..models.result_field import FieldStatus, Magnification, ResultField
from ..models.specimen import Specimen
from ..schemas.field import Detection, FieldAnalysisResponse
from . import specimen_service
from .ai_integration_service import (
    MIME_TO_FORMAT,
    AIIntegrationService,
    EngineInference,
    normalizeClassNames,
)
from .consent_check import requireProcessingConsent
from .specimen_access import CAPTURE_PHASE_RESULT_STATUSES, getAssignedSpecimen

log = logging.getLogger(__name__)


class FieldCaptureService(AIIntegrationService):
    """Adds one analysed field per upload while a specimen is being captured."""

    async def handleFieldUpload(
        self,
        specimenId: uuid.UUID,
        uploaderId: uuid.UUID,
        file: UploadFile,
        magnification: Magnification,
        request: Request | None,
    ) -> FieldAnalysisResponse:
        """Validate, analyse and store one field of a specimen.

        The first field creates the specimen's result in `CAPTURING` and starts a
        specimen that has not been started, as "Begin Analysis" would. Each field
        gets the next number (numbers are never reused, even after a discard), the
        AI counts, boxes and confidence for that image, and its own Smart
        Diagnosis preview. The field starts `ANALYZED`: it counts toward nothing
        until the MedTech accepts it. Nothing is saved unless the image is both
        analysed and stored.

        Args:
            specimenId: the specimen the field belongs to.
            uploaderId: the calling MedTech, from their token.
            file: the multipart image.
            magnification: the objective the field was captured at.
            request: the inbound request, for the audit row's client IP.

        Returns:
            The new field's analysis, with the accepted-field count and minimum.

        Raises:
            ImageFormatError: `INVALID_IMAGE_FORMAT`, if the file isn't a genuine
                JPEG or PNG of its declared type.
            ImageTooLargeError: `IMAGE_TOO_LARGE`, if over the size cap.
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if `specimenId` doesn't exist.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if it isn't the caller's.
            ConflictException: `SPECIMEN_REJECTED`, if the specimen was rejected;
                `RESULT_NOT_EDITABLE`, if its result exists and is not `CAPTURING`;
                `CONSENT_REFUSED`, if the patient refused processing.
            ImageResolutionError: `INVALID_IMAGE_RESOLUTION`, if below 640x480.
            ImageNotMicroscopyError: `IMAGE_NOT_MICROSCOPY` (422), if the AI
                engine's input gate says it isn't a microscope image.
            ImageExposureError: `IMAGE_EXPOSURE` (422), if the gate says it is
                too dark, blown out, or mostly black.
            AIAnalysisError: `AI_ANALYSIS_FAILED` (503), if inference failed.
            StorageError: `STORAGE_ERROR` (503), if the image couldn't be stored.
        """
        contentType = file.content_type or ""
        self._validateFormat(contentType)
        rawBytes = await self._readWithinLimit(file)
        specimen, result = await self._requireFieldUploadAllowed(specimenId, uploaderId, request)
        width, height = await self._validateImage(rawBytes, contentType)
        imageBytes = await self._stripMetadata(rawBytes, contentType)
        # Analyse before storing, so a failed analysis leaves nothing behind.
        inference = await self._runInference(specimenId, imageBytes)
        counts = self._countsFrom(inference, specimenId)
        detections, imageWidth, imageHeight, droppedBoxes = toFieldDetections(inference, width, height)
        if droppedBoxes:
            log.warning(
                "Dropped %s unusable AI box(es) for specimen %s; counts kept as reported",
                droppedBoxes, specimenId,
            )

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

        if result is None:
            result = await self._createCapturingResult(specimen, imageId)
        field = ResultField(
            fieldId=uuid.uuid4(),
            resultId=result.resultId,
            specimenId=specimenId,
            imageId=imageId,
            fieldNo=await self._nextFieldNo(result.resultId),
            magnification=magnification,
            status=FieldStatus.ANALYZED,
            aiFindings=counts,
            detections=detections,
            imageWidth=imageWidth,
            imageHeight=imageHeight,
            confidenceScores=normalizeClassNames(getattr(inference, "confidence_scores", None) or {}),
            effectiveCounts=dict(counts),
            modelVersion=settings.aiModelVersion,
            version=1,
        )
        field.smartDiagnosis, field.smartDiagnosisUnavailable = self._previewSmartDiagnosis(counts, field)
        self.db.add(field)
        await self.db.flush([field])
        startedAnalysis = await specimen_service.markProcessing(self.db, specimen, uploaderId, request)

        await self.auditLogger.record(
            db=self.db,
            eventType="IMAGE_UPLOADED",
            entityType="image",
            entityId=imageId,
            userId=uploaderId,
            detailJson={
                "specimen_id": str(specimenId),
                "result_id": str(result.resultId),
                "field_id": str(field.fieldId),
                "field_no": field.fieldNo,
                "magnification": str(magnification),
                "file_format": MIME_TO_FORMAT[contentType],
                "width_px": width,
                "height_px": height,
                "file_size_bytes": len(imageBytes),
                "started_analysis": startedAnalysis,
                "dropped_boxes": droppedBoxes,
            },
            request=request,
        )
        await self.db.commit()

        return FieldAnalysisResponse(
            fieldId=field.fieldId,
            fieldNo=field.fieldNo,
            magnification=magnification,
            resultId=result.resultId,
            imageId=imageId,
            status=FieldStatus.ANALYZED,
            aiFindings=counts,
            detections=[Detection(**box) for box in detections],
            imageWidth=imageWidth,
            imageHeight=imageHeight,
            confidenceScores=field.confidenceScores,
            smartDiagnosisPreview=field.smartDiagnosis,
            smartDiagnosisUnavailable=field.smartDiagnosisUnavailable,
            acceptedFieldCount=result.fieldCount or 0,
            minFields=settings.minFieldsPerSpecimen,
        )

    # ── Private helpers ──────────────────────────────────────────────────

    async def _requireFieldUploadAllowed(
        self, specimenId: uuid.UUID, uploaderId: uuid.UUID, request: Request | None
    ) -> tuple[Specimen, AnalysisResult | None]:
        """Allow a field only for the assigned MedTech, while the result is being captured.

        A result that exists in any other status, including a single-image result
        still waiting for confirmation, takes no new fields until the web
        workspace reopens capture (`POST /results/{id}/reopen-capture`).

        Returns:
            The specimen (locked until the transaction ends) and its result, if any.
        """
        specimen = await getAssignedSpecimen(self.db, specimenId, uploaderId)
        if specimen.status == "REJECTED":
            raise ConflictException(
                code="SPECIMEN_REJECTED",
                message="This specimen was rejected, so no image can be added to it.",
            )
        result = (
            await self.db.execute(select(AnalysisResult).where(AnalysisResult.specimenId == specimenId))
        ).scalar_one_or_none()
        if result is not None and result.status not in CAPTURE_PHASE_RESULT_STATUSES:
            raise ConflictException(
                code="RESULT_NOT_EDITABLE",
                message="Fields can only be added while this specimen is being captured.",
            )
        await requireProcessingConsent(
            self.db, specimen, uploaderId, self.auditLogger, action="IMAGE_UPLOAD", request=request
        )
        return specimen, result

    async def _createCapturingResult(self, specimen: Specimen, imageId: uuid.UUID) -> AnalysisResult:
        """Create the specimen's result for its first field.

        `imageId` points at the first image so readers that still show one image
        per result have one to show.
        """
        result = AnalysisResult(
            specimenId=specimen.specimenId,
            imageId=imageId,
            patientId=await self._patientIdFor(specimen),
            status=ResultStatus.CAPTURING,
            modelVersion=settings.aiModelVersion,
            smartDiagnosisUnavailable=False,
            fieldCount=0,
        )
        self.db.add(result)
        await self.db.flush([result])
        return result

    async def _nextFieldNo(self, resultId: uuid.UUID) -> int:
        """Return the next field number for the result.

        One more than the highest number so far, discarded fields included, so a
        number always means the same capture.
        """
        highest = (
            await self.db.execute(
                select(func.max(ResultField.fieldNo)).where(ResultField.resultId == resultId)
            )
        ).scalar_one_or_none()
        return (highest or 0) + 1

    def _countsFrom(self, inference: EngineInference, specimenId: uuid.UUID) -> dict[str, int]:
        """The AI counts per class, or `AI_ANALYSIS_FAILED` if the engine gave none."""
        try:
            return normalizeClassNames(inference.particles)
        except Exception as exc:
            log.warning("AI inference returned no counts for specimen %s", specimenId, exc_info=True)
            raise AIAnalysisError() from exc

    def _previewSmartDiagnosis(
        self, counts: dict[str, int], field: ResultField
    ) -> tuple[dict | None, bool]:
        """The field's own Smart Diagnosis preview and whether it is unavailable.

        Best effort, like the single-image preview: a failure never blocks the
        upload, it only marks this field's preview unavailable. A field with
        nothing detected gets no preview.
        """
        if not counts:
            return None, False
        try:
            return self._computeSmartDiagnosis(counts), False
        except Exception as exc:
            log.warning("Smart Diagnosis failed for field %s: %s", field.fieldId, exc)
            return None, True


class FieldBoxes(NamedTuple):
    """The boxes stored for a field and the image size they are relative to."""

    boxes: list[dict[str, Any]]
    width: int
    height: int
    dropped: int
    """Boxes the engine returned that could not be stored (see `toFieldDetections`)."""


def toFieldDetections(
    inference: EngineInference, fallbackWidth: int, fallbackHeight: int
) -> FieldBoxes:
    """Convert the engine's pixel boxes to stored boxes in percent of the image.

    The engine reports each detection as `class_name`, `confidence` and `bbox`
    `[x1, y1, x2, y2]` in pixels of the image it analysed, which is the image
    stored. Each box gets an id assigned here, stable for the life of the field.
    A box reaching past an edge is trimmed to the image. A box that is not a set
    of finite numbers, or covers no part of the image, is dropped and counted:
    the AI counts stay exactly as the engine reported them, so the MedTech sees a
    class with fewer boxes than its count and reconciles it on the web. Engines
    before v0.2.0 return no detections; this route only runs with the flag on,
    which refuses such an engine at startup, so that case gives no boxes rather
    than a failure only as a safeguard.

    Args:
        inference: the engine's `InferenceResult`.
        fallbackWidth: the validated image width, used if the engine gives none.
        fallbackHeight: the validated image height, likewise.
    """
    width = _positiveInt(getattr(inference, "image_width", None)) or fallbackWidth
    height = _positiveInt(getattr(inference, "image_height", None)) or fallbackHeight
    raw = getattr(inference, "detections", None)
    if not isinstance(raw, list):
        return FieldBoxes([], width, height, 0)

    boxes = []
    dropped = 0
    for detection in raw:
        box = _toStoredBox(detection, width, height)
        if box is None:
            dropped += 1
        else:
            boxes.append(box)
    return FieldBoxes(boxes, width, height, dropped)


def _toStoredBox(detection: object, width: int, height: int) -> dict[str, Any] | None:
    # One engine detection as a stored box, or None if it can't be drawn.
    className = getattr(detection, "class_name", None)
    try:
        x1, y1, x2, y2 = (float(v) for v in getattr(detection, "bbox", None))
        confidence = float(getattr(detection, "confidence", None))
    except (TypeError, ValueError):
        return None
    if not isinstance(className, str):
        return None
    # Finiteness first: min/max would quietly turn a NaN into an edge.
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2, confidence)):
        return None
    x1, x2 = max(0.0, min(x1, width)), max(0.0, min(x2, width))
    y1, y2 = max(0.0, min(y1, height)), max(0.0, min(y2, height))
    if x2 <= x1 or y2 <= y1:
        return None
    return {
        "id": uuid.uuid4().hex,
        "particleType": className.replace("-", "_"),
        "confidence": confidence,
        "x": 100 * x1 / width,
        "y": 100 * y1 / height,
        "w": 100 * (x2 - x1) / width,
        "h": 100 * (y2 - y1) / height,
    }


def _positiveInt(value: object) -> int | None:
    # Accept only a real positive integer size; anything else means "not given".
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
