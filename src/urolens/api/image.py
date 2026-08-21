"""
Image upload and discard routes — T2.7

Routes
------
POST /api/v1/images/upload              — multipart upload, returns analysis result
POST /api/v1/images/{image_id}/discard  — mark image DISCARDED for retake flow
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.enums import UserRole
from ..core.rbac import RequireRole
from ..schemas.image import AnalysisResultResponse, ImageDiscardResponse
from ..services.ai_integration_service import AIIntegrationService
from ..services.image_retake_service import ImageRetakeService

router = APIRouter(prefix="/api/v1/images", tags=["images"])
_retake_service = ImageRetakeService()
_medtech = RequireRole([UserRole.MEDTECH])


async def get_ai_integration_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> AIIntegrationService:
    return AIIntegrationService(db=db, audit_logger=audit_logger)


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
    claims: dict = Depends(_medtech),
    service: AIIntegrationService = Depends(get_ai_integration_service),
) -> AnalysisResultResponse:
    uploader_id = uuid.UUID(claims["user_id"])
    result = await service.handle_upload(specimen_id, uploader_id, file, request)
    return AnalysisResultResponse(
        id=result.result_id,
        result_id=result.result_id,
        specimen_id=result.specimen_id,
        image_id=result.image_id,
        status=result.status,
        ai_findings=result.ai_findings,
        flagged_anomalies=result.flagged_anomalies,
        smart_diagnosis=result.smart_diagnosis,
    )


@router.post(
    "/{image_id}/discard",
    response_model=ImageDiscardResponse,
    status_code=status.HTTP_200_OK,
    summary="Discard an image so the MedTech can retake (T2.7)",
)
async def discard_image(
    image_id: uuid.UUID,
    request: Request,
    claims: dict = Depends(_medtech),
) -> ImageDiscardResponse:
    medtech_id = uuid.UUID(claims["user_id"])
    result = await _retake_service.discard_and_retake(
        image_id=image_id,
        medtech_id=medtech_id,
        request=request,
    )
    return ImageDiscardResponse(**result)
