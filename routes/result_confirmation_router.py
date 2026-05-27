"""FastAPI router — result confirmation endpoints.

TASK-MOB-09-4: POST /api/v1/results/{id}/confirm  (MEDTECH role)
TASK-MOB-09-5: GET  /api/v1/results/{id}          (full result + ai_findings + smart_diagnosis)

STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/app/routers/result_confirmation_router.py

SOLID:
  - Single Responsibility: HTTP transport / request-response mapping only.
  - Dependency Inversion: depends on IResultConfirmationService.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies.auth import get_current_user, require_role
from app.dependencies.db import get_db_session
from app.dependencies.services import get_result_confirmation_service
from app.models.user import User
from app.schemas.result_confirmation import (
    ConfirmResultRequest,
    FullResultOut,
    ResultConfirmationOut,
)
from app.services.result_confirmation_service import (
    IResultConfirmationService,
    PendingRetakeError,
    ResultAlreadyConfirmedError,
    ResultNotFoundError,
)

router = APIRouter(prefix="/api/v1/results", tags=["Result Confirmation"])


# ── GET /api/v1/results/{id} ──────────────────────────────────────────────────

@router.get(
    "/{result_id}",
    response_model=FullResultOut,
    summary="Get full analysis result with AI findings and Smart Diagnosis",
    status_code=status.HTTP_200_OK,
)
async def get_full_result(
    result_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: IResultConfirmationService = Depends(get_result_confirmation_service),
) -> FullResultOut:
    """Return the full AnalysisResult including ai_findings and smart_diagnosis.

    TASK-MOB-09-5
    Accessible by MEDTECH and SUPERVISOR roles.
    """
    try:
        result = await service.get_full_result(result_id)
    except ResultNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return FullResultOut.model_validate(result)


# ── POST /api/v1/results/{id}/confirm ─────────────────────────────────────────

@router.post(
    "/{result_id}/confirm",
    response_model=ResultConfirmationOut,
    summary="MedTech confirms an AI analysis result",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("MEDTECH"))],
)
async def confirm_result(
    result_id: uuid.UUID,
    body: ConfirmResultRequest,
    current_user: User = Depends(get_current_user),
    service: IResultConfirmationService = Depends(get_result_confirmation_service),
) -> ResultConfirmationOut:
    """MedTech formally confirms an AI analysis result.

    TASK-MOB-09-4
    - Validates no pending retake is outstanding.
    - Sets result status → PENDING_SUPERVISOR_APPROVAL.
    - Triggers Smart Diagnosis automatically.
    - Writes a RESULT_CONFIRMED audit event.
    """
    try:
        confirmation = await service.confirm_result(
            result_id=result_id,
            confirmed_by=current_user.id,
            notes=body.notes,
        )
    except ResultNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ResultAlreadyConfirmedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except PendingRetakeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return ResultConfirmationOut.model_validate(confirmation)