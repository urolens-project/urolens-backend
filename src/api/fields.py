"""Multi-field workflow routes (proposal section 10.4) — T2.3 to T2.7.

Routes
------
POST /api/v1/fields                               — Capture a field (mobile lane)
POST /api/v1/fields/{field_id}/accept             — Proceed (mobile lane)
POST /api/v1/fields/{field_id}/discard            — Discard or exclude (shared)
POST /api/v1/results/{id}/fields/submit           — Submit for Verification (mobile lane)
GET  /api/v1/results/{id}/fields                  — Field list (web lane)
GET  /api/v1/fields/{field_id}                    — One field in full (web lane)
PUT  /api/v1/fields/{field_id}/annotations        — Save box edits (web lane)
POST /api/v1/fields/{field_id}/override           — Per-field override (web lane)
POST /api/v1/fields/{field_id}/override/revert    — Revert an override (web lane)
POST /api/v1/fields/{field_id}/verify             — Mark as checked (web lane)
POST /api/v1/results/{id}/reopen-capture          — Capture more fields (web lane)
GET  /api/v1/results/{id}/summary                 — Verification summary (web lane)

Every route is registered with its final role and response model, and answers
404 FEATURE_DISABLED while `fieldWorkflowEnabled` is off. Routes a lane has not
built yet answer 501 NOT_IMPLEMENTED when it is on. The flag check is a
dependency so it also wins over a body-validation 422.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger, getAuditLogger
from ..core.config import settings
from ..core.database import getDb
from ..core.enums import UserRole
from ..core.exceptions import FeatureDisabledError, NotImplementedYetError
from ..core.rbac import RequireRole
from ..models.result_field import Magnification
from ..schemas.field import (
    DiscardFieldRequest,
    FieldActionResponse,
    FieldAnalysisResponse,
    FieldDetail,
    FieldListResponse,
    FieldOverrideRequest,
    ResultStatusResponse,
    RevertOverrideRequest,
    SaveAnnotationsRequest,
    VerificationSummaryResponse,
)
from ..services.field_capture_service import FieldCaptureService

router = APIRouter(tags=["fields"])
_medtech = RequireRole([UserRole.MEDTECH])
_medtechOrSupervisor = RequireRole([UserRole.MEDTECH, UserRole.SUPERVISOR])


def requireFieldWorkflow() -> None:
    """Dependency: 404 `FEATURE_DISABLED` while the multi-field flag is off."""
    if not settings.fieldWorkflowEnabled:
        raise FeatureDisabledError()


# ── Dependency factories ──

async def getFieldCaptureService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> FieldCaptureService:
    """FastAPI dependency constructing a request-scoped `FieldCaptureService`."""
    return FieldCaptureService(db=db, auditLogger=auditLogger)


@router.post(
    "/api/v1/fields",
    response_model=FieldAnalysisResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Capture a field: upload one image and analyse it (mobile lane)",
)
async def captureField(
    request: Request,
    specimenId: uuid.UUID = Form(..., alias="specimen_id"),
    file: UploadFile = File(..., description="JPEG or PNG, minimum 640×480"),
    magnification: Magnification = Form(Magnification.HPF, description="Objective of the field"),
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
    _service: FieldCaptureService = Depends(getFieldCaptureService),
) -> FieldAnalysisResponse:
    """Add one analysed field to a specimen; see `FieldCaptureService.handleFieldUpload`."""
    uploaderId = uuid.UUID(claims["user_id"])
    return await _service.handleFieldUpload(specimenId, uploaderId, file, magnification, request)


@router.post(
    "/api/v1/fields/{field_id}/accept",
    response_model=FieldActionResponse,
    summary="Proceed with an analysed field (mobile lane)",
)
async def acceptField(
    field_id: uuid.UUID,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """Move a field from ANALYZED to ACCEPTED while the result is CAPTURING."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/fields/{field_id}/discard",
    response_model=FieldActionResponse,
    summary="Discard or exclude a field (shared)",
)
async def discardField(
    field_id: uuid.UUID,
    body: DiscardFieldRequest | None = None,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """Discard a field. Mobile path while CAPTURING (reason optional); web path while
    PENDING_CONFIRM or RETURNED_FOR_CORRECTION (reason required)."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/results/{id}/fields/submit",
    response_model=ResultStatusResponse,
    summary="Submit the field list for verification (mobile lane)",
)
async def submitFields(
    id: uuid.UUID,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> ResultStatusResponse:
    """CAPTURING to PENDING_CONFIRM; 409 FIELDS_UNDECIDED or FIELDS_BELOW_MINIMUM."""
    raise NotImplementedYetError()


@router.get(
    "/api/v1/results/{id}/fields",
    response_model=FieldListResponse,
    summary="List a result's fields (web lane)",
)
async def listFields(
    id: uuid.UUID,
    claims: dict = Depends(_medtechOrSupervisor),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldListResponse:
    """Fields with counts, status, flags and one signed image URL each; no detections."""
    raise NotImplementedYetError()


@router.get(
    "/api/v1/fields/{field_id}",
    response_model=FieldDetail,
    summary="One field in full, with detections (web lane)",
)
async def getField(
    field_id: uuid.UUID,
    claims: dict = Depends(_medtechOrSupervisor),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldDetail:
    """Detections and box edits for one field; records the open that `verify` needs."""
    raise NotImplementedYetError()


@router.put(
    "/api/v1/fields/{field_id}/annotations",
    response_model=FieldActionResponse,
    summary="Save a field's box edits (web lane)",
)
async def saveAnnotations(
    field_id: uuid.UUID,
    body: SaveAnnotationsRequest,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """Replace the field's edit list; 409 FIELD_VERSION_CONFLICT on a stale version."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/fields/{field_id}/override",
    response_model=FieldActionResponse,
    summary="Override one class count on a field (web lane)",
)
async def overrideFieldParameter(
    field_id: uuid.UUID,
    body: FieldOverrideRequest,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """Numeric override with a required reason; locks box editing for that class."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/fields/{field_id}/override/revert",
    response_model=FieldActionResponse,
    summary="Revert a field override (web lane)",
)
async def revertFieldOverride(
    field_id: uuid.UUID,
    body: RevertOverrideRequest,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """Adds a REVERT row; the class counts boxes again."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/fields/{field_id}/verify",
    response_model=FieldActionResponse,
    summary="Mark a field as checked (web lane)",
)
async def verifyField(
    field_id: uuid.UUID,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> FieldActionResponse:
    """409 FIELD_NOT_OPENED unless the same user opened the field since its last correction."""
    raise NotImplementedYetError()


@router.post(
    "/api/v1/results/{id}/reopen-capture",
    response_model=ResultStatusResponse,
    summary="Send a result back to the phone for more fields (web lane)",
)
async def reopenCapture(
    id: uuid.UUID,
    claims: dict = Depends(_medtech),
    _enabled: None = Depends(requireFieldWorkflow),
) -> ResultStatusResponse:
    """PENDING_CONFIRM or RETURNED_FOR_CORRECTION back to CAPTURING; fields are kept."""
    raise NotImplementedYetError()


@router.get(
    "/api/v1/results/{id}/summary",
    response_model=VerificationSummaryResponse,
    summary="Verification summary: final averages before confirming (web lane)",
)
async def getVerificationSummary(
    id: uuid.UUID,
    claims: dict = Depends(_medtechOrSupervisor),
    _enabled: None = Depends(requireFieldWorkflow),
) -> VerificationSummaryResponse:
    """Per-class averages, corrections, focal findings and the diagnosis preview."""
    raise NotImplementedYetError()
