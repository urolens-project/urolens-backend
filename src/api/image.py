"""Image upload and discard routes — T2.7

Routes
------
POST /api/v1/images/upload              — multipart upload, returns analysis result
POST /api/v1/images/{image_id}/discard  — mark image DISCARDED for retake flow
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger, getAuditLogger
from ..core.database import getDb
from ..core.enums import UserRole
from ..core.rbac import RequireRole
from ..schemas.image import AnalysisResultResponse, ImageDiscardResponse
from ..services.ai_integration_service import AIIntegrationService
from ..services.image_retake_service import ImageRetakeService

router = APIRouter(prefix="/api/v1/images", tags=["images"])
_medtech = RequireRole([UserRole.MEDTECH])


async def getAiIntegrationService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> AIIntegrationService:
    """FastAPI dependency constructing a request-scoped `AIIntegrationService`."""
    return AIIntegrationService(db=db, auditLogger=auditLogger)


async def getImageRetakeService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> ImageRetakeService:
    """FastAPI dependency constructing a request-scoped `ImageRetakeService`."""
    return ImageRetakeService(db=db, auditLogger=auditLogger)


@router.post(
    "/upload",
    response_model=AnalysisResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a microscopy image and trigger AI inference (T2.7)",
)
async def uploadImage(
    request: Request,
    specimenId: uuid.UUID = Form(...),
    file: UploadFile = File(..., description="JPEG or PNG, minimum 640×480"),
    claims: dict = Depends(_medtech),
    _service: AIIntegrationService = Depends(getAiIntegrationService),
) -> AnalysisResultResponse:
    """Upload a microscopy image for a specimen and run AI inference; see
    `AIIntegrationService.handle_upload`.
    """
    uploaderId = uuid.UUID(claims["user_id"])
    result = await _service.handleUpload(specimenId, uploaderId, file, request)
    return AnalysisResultResponse(
        id=result.resultId,
        resultId=result.resultId,
        specimenId=result.specimenId,
        imageId=result.imageId,
        status=result.status,
        aiFindings=result.aiFindings,
        flaggedAnomalies=result.flaggedAnomalies,
        smartDiagnosis=result.smartDiagnosis,
    )


@router.post(
    "/{image_id}/discard",
    response_model=ImageDiscardResponse,
    status_code=status.HTTP_200_OK,
    summary="Discard an image so the MedTech can retake (T2.7)",
)
async def discardImage(
    image_id: uuid.UUID,
    request: Request,
    claims: dict = Depends(_medtech),
    _service: ImageRetakeService = Depends(getImageRetakeService),
) -> ImageDiscardResponse:
    """Discard the current image so the MedTech can retake; see
    `ImageRetakeService.discard_and_retake`.
    """
    medtechId = uuid.UUID(claims["user_id"])
    result = await _service.discardAndRetake(
        imageId=image_id,
        medtechId=medtechId,
        request=request,
    )
    return ImageDiscardResponse(**result)
