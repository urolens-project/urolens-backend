"""Image Retake Service — T2.7 (SQLAlchemy `AsyncSession` implementation)

Handles the Retake flow: the MedTech discards the current image and the capture
screen re-opens so a new image can be uploaded.

Responsibilities
----------------
- Validate the caller may discard it: the specimen must be assigned to them,
  and its result must not be submitted, approved or released yet (SEC-2).
- Validate the image can be discarded (must be ACTIVE, not already DISCARDED/REPLACED).
- Mark the image as DISCARDED.
- Emit the IMAGE_DISCARDED audit event via the centralized `AuditLogger`.
- The AnalysisResult row remains intact — it will be updated when the new image
  is uploaded and inference runs again.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import ConflictError, ConflictException, NotFoundError
from ..models.analysis_result import AnalysisResult
from ..models.image import Image
from .specimen_access import (
    MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES,
    getAssignedSpecimen,
)


class ImageRetakeService:
    """Handles the Retake flow: discarding the current image so a new one
    can be uploaded, per the module docstring above.
    """

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        self.db = db
        self.auditLogger = auditLogger

    async def discardAndRetake(
        self,
        imageId: uuid.UUID,
        medtechId: uuid.UUID,
        request: Any = None,
    ) -> dict:
        """Mark the image DISCARDED so the MedTech can submit a new one.

        Returns a dict with image_id, status, discarded_at.

        Raises:
            NotFoundError: `NOT_FOUND`, if the image doesn't exist.
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if its specimen doesn't.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen isn't
                assigned to `medtechId`.
            ConflictError: `CONFLICT`, if the image is already DISCARDED or
                REPLACED.
            ConflictException: `RESULT_NOT_EDITABLE`, if the specimen's result
                has already been submitted, approved or released.
        """
        image = await self.db.get(Image, imageId)
        if image is None:
            raise NotFoundError(f"Image {imageId} not found.")
        # Ownership before any state check, so a non-owner learns nothing.
        # Then re-read the image under the specimen lock, so two racing
        # discards (or a discard racing an upload) can't both pass.
        await getAssignedSpecimen(self.db, image.specimenId, medtechId)
        await self.db.refresh(image)

        if image.status == "DISCARDED":
            raise ConflictError("This image has already been discarded.")

        if image.status == "REPLACED":
            raise ConflictError(
                "This image has been superseded by a newer upload. "
                "Please upload a new image."
            )

        await self._requireResultStillEditable(image.specimenId)

        # `images.updated_at` isn't mapped on `Image` (unconfirmed live column
        # — see the model's schema-drift note), so unlike the prior
        # Supabase-REST version's try/retry-without-updated_at dance, this
        # simply never references it: SQLAlchemy only ever writes columns it
        # knows about.
        nowUtc = datetime.now(UTC)
        image.status = "DISCARDED"
        image.discardedAt = nowUtc

        await self.auditLogger.record(
            eventType="IMAGE_DISCARDED",
            entityType="image",
            entityId=imageId,
            userId=medtechId,
            detailJson={"specimen_id": str(image.specimenId)},
            request=request,
        )

        await self.db.commit()

        return {
            "imageId": str(imageId),
            "status": "DISCARDED",
            "discardedAt": nowUtc.isoformat(),
        }

    async def _requireResultStillEditable(self, specimenId: uuid.UUID) -> None:
        # Discarding the image behind a submitted/approved/released result
        # would leave a finalised record pointing at a discarded image.
        result = (
            await self.db.execute(
                select(AnalysisResult).where(AnalysisResult.specimenId == specimenId)
            )
        ).scalar_one_or_none()
        if result is not None and result.status not in MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES:
            raise ConflictException(
                code="RESULT_NOT_EDITABLE",
                message="This result has already been submitted, so its image can't be discarded.",
            )
