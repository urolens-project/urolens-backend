# src/urolens/api/images.py
"""
Image upload and discard routes — T2.7

Routes
------
POST /api/v1/images/upload           — multipart upload, triggers inference
POST /api/v1/images/{image_id}/discard — discard + retake flow
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import BaseModel, field_serializer
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.exceptions import ImageFormatError, ImageResolutionError, StorageError
from ..middleware.rbac import RequireRole
from ..models.user import User, UserRole
from ..services.ai_integration_service import AIIntegrationService
from ..services.image_retake_service import ImageRetakeService

router = APIRouter(prefix="/api/v1/images", tags=["images"])


# ── Response schemas ──────────────────────────────────────────────────────────

class AnalysisResultResponse(BaseModel):
    id: uuid.UUID
    specimen_id: uuid.UUID
    image_id: uuid.UUID
    status: str
    ai_findings: dict | None

    model_config = {"from_attributes": True}


class ImageDiscardResponse(BaseModel):
    id: uuid.UUID
    status: str
    discarded_at: str | None

    model_config = {"from_attributes": True}

    @field_serializer("discarded_at")
    def serialize_dt(self, v: object) -> str | None:
        return v.isoformat() if v else None  # type: ignore[attr-defined]


# ── Dependency factories ──────────────────────────────────────────────────────

def get_ai_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> AIIntegrationService:
    return AIIntegrationService(db=db, audit_logger=audit_logger)


def get_retake_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
    ai_svc: AIIntegrationService = Depends(get_ai_service),
) -> ImageRetakeService:
    return ImageRetakeService(db=db, audit_logger=audit_logger, ai_integration_service=ai_svc)


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
    current_user: User = Depends(RequireRole([UserRole.MEDTECH])),
    service: AIIntegrationService = Depends(get_ai_service),
) -> AnalysisResultResponse:
    """
    Accepts a multipart image upload from the MedTech mobile app.

    Validation (mirrors client-side checks):
    - MIME type must be image/jpeg or image/png
    - Resolution must be ≥ 640×480

    On success the image is stored in S3 and AI inference is triggered
    synchronously. If inference fails the AnalysisResult row is created
    with status=FAILED — the upload itself is still accepted (HTTP 201).
    """
    result = await service.handle_upload(
        specimen_id=specimen_id,
        uploader_id=current_user.id,
        file=file,
        request=request,
    )
    return AnalysisResultResponse.model_validate(result)


@router.post(
    "/{image_id}/discard",
    response_model=ImageDiscardResponse,
    status_code=status.HTTP_200_OK,
    summary="Discard an image so the MedTech can retake (T2.7)",
)
async def discard_image(
    image_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(RequireRole([UserRole.MEDTECH])),
    service: ImageRetakeService = Depends(get_retake_service),
) -> ImageDiscardResponse:
    """
    Marks the image as DISCARDED. The MedTech is returned to the capture screen.
    The linked AnalysisResult row is preserved until a new image is uploaded.

    The client should show a blocking DiscardConfirmationModal before calling
    this endpoint.
    """
    discarded = await service.discard_and_retake(
        image_id=image_id,
        medtech_id=current_user.id,
        request=request,
    )
    return ImageDiscardResponse.model_validate(discarded)