"""Image Retake Service — T2.7 (SQLAlchemy `AsyncSession` implementation)

Handles the Retake flow: the MedTech discards the current image and the capture
screen re-opens so a new image can be uploaded.

Responsibilities
----------------
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

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import ConflictError, NotFoundError
from ..models.image import Image


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
        ------
        NotFoundError  — image not found
        ConflictError  — image is already DISCARDED or REPLACED
        """
        image = await self.db.get(Image, imageId)
        if image is None:
            raise NotFoundError(f"Image {imageId} not found.")

        if image.status == "DISCARDED":
            raise ConflictError("This image has already been discarded.")

        if image.status == "REPLACED":
            raise ConflictError(
                "This image has been superseded by a newer upload. "
                "Please upload a new image."
            )

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
