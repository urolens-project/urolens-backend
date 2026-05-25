from __future__ import annotations

"""
Image Retake Service — T2.7

Handles the Retake flow: the MedTech discards the current image and the capture
screen re-opens so a new image can be uploaded.

Responsibilities
----------------
- Validate the image can be discarded (must be ACTIVE, not already DISCARDED/REPLACED).
- Delegate the actual status update to AIIntegrationService.discard_image().
- Emit the IMAGE_DISCARDED audit event.
- The AnalysisResult row remains intact — it will be updated when the new image
  is uploaded and inference runs again.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import ConflictError, NotFoundError
from ..models.image import Image, ImageStatus
from .ai_integration_service import AIIntegrationService


class ImageRetakeService:
    def __init__(
        self,
        db: AsyncSession,
        audit_logger: AuditLogger,
        ai_integration_service: AIIntegrationService,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger
        self.ai_svc = ai_integration_service

    async def discard_and_retake(
        self,
        image_id: uuid.UUID,
        medtech_id: uuid.UUID,
        request: Any,
    ) -> Image:
        """
        Mark the image DISCARDED so the MedTech can submit a new one.

        Raises
        ------
        NotFoundError  — image not found
        ConflictError  — image is already DISCARDED or REPLACED
        """
        image = await self.db.get(Image, image_id)
        if image is None:
            raise NotFoundError(f"Image {image_id} not found.")

        if image.status == ImageStatus.DISCARDED:
            raise ConflictError("This image has already been discarded.")

        if image.status == ImageStatus.REPLACED:
            raise ConflictError(
                "This image has been superseded by a newer upload. "
                "Please upload a new image."
            )

        discarded = await self.ai_svc.discard_image(
            image_id=image_id,
            discarded_by=medtech_id,
            request=request,
        )

        await self.audit_logger.record(
            event_type="IMAGE_DISCARDED",
            entity_type="image",
            entity_id=image_id,
            user_id=medtech_id,
            detail_json={"specimen_id": str(image.specimen_id)},
            db=self.db,
            request=request,
        )

        await self.db.commit()
        return discarded
