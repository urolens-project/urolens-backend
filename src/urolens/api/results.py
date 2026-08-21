"""
Result confirm/override (plan row 6) and supervisor review/approval
(plan row 7) routes.

History: this file initially (step 5a) contained only confirm/override —
the row-7-shaped routes that were here before 5a were bridged to the old,
unported `app.services.result_review_service` and removed to avoid
colliding with the still-live `app/api/results.py` at the same
`/api/v1/results` prefix. Row 7 is now properly ported (step 5b) — a real
SQLAlchemy service, not a bridge to the old one — and the equivalent
routes are re-added here for real, with the old ones removed from
`app/api/results.py` in the same change. `app/api/results.py` keeps only
its `GET /{result_id}/smart-diagnosis` route, which belongs to neither
row 6 nor row 7 (see CHANGELOG.md).

Route registration order matters here: `GET /pending`, `/approved-today`,
`/escalated`, and `/supervisor/stats` are literal paths and must be
registered before the catch-all `GET /{result_id}`, or that catch-all
would shadow them (this is why `app/api/results.py` had a "supervisor
endpoints" section ordered the same way before this port).
"""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.rbac import RequireRole

from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.enums import UserRole
from ..schemas.result_review import (
    AnnotationRequest,
    AnnotationResponse,
    ApproveRequest,
    ApproveResponse,
    ApprovedTodayListResponse,
    EscalateRequest,
    EscalateResponse,
    EscalatedListResponse,
    FullResultDetail,
    PendingResultListResponse,
    ReturnRequest,
    ReturnResponse,
    SupervisorStatsResponse,
)
from ..services.manual_override_service import ManualOverrideService
from ..services.notification_service import NotificationService
from ..services.result_confirmation_service import ResultConfirmationService
from ..services.result_review_service import ResultReviewService
from ..services.smart_diagnosis_service import SmartDiagnosisService

router = APIRouter(prefix="/api/v1/results", tags=["results"])

_supervisor = RequireRole([UserRole.SUPERVISOR])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ConfirmResultResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    confirmed_by: uuid.UUID
    confirmed_at: datetime

    model_config = {"from_attributes": True}


class OverrideRequest(BaseModel):
    parameter: str = Field(..., min_length=1, max_length=100)
    corrected_value: float = Field(..., ge=0)
    rationale: Optional[str] = Field("No rationale provided", max_length=2000)
    original_ai_value: float = Field(..., ge=0)

    @field_validator("parameter")
    @classmethod
    def parameter_no_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("parameter must not be blank")
        return v.strip()


class OverrideResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    parameter: str
    original_ai_value: float
    corrected_value: float
    rationale: str
    overridden_by: uuid.UUID
    overridden_at: datetime

    model_config = {"from_attributes": True}


# ── Dependency factories (DIP) ────────────────────────────────────────────────

async def get_notif_service(
    db: AsyncSession = Depends(get_db),
) -> NotificationService:
    return NotificationService(db=db)


async def get_confirmation_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
    notif_service: NotificationService = Depends(get_notif_service),
) -> ResultConfirmationService:
    smart_diag = SmartDiagnosisService(
        audit_logger=audit_logger,
        notif_service=notif_service,
    )
    return ResultConfirmationService(
        db=db,
        audit_logger=audit_logger,
        smart_diagnosis_service=smart_diag,
        notif_service=notif_service,
    )


async def get_override_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> ManualOverrideService:
    return ManualOverrideService(db=db, audit_logger=audit_logger)


async def get_result_review_service(
    db: AsyncSession = Depends(get_db),
) -> ResultReviewService:
    return ResultReviewService(db=db)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{id}/confirm", response_model=ConfirmResultResponse, status_code=200)
async def confirm_result(
    id: uuid.UUID,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH])),
    service: ResultConfirmationService = Depends(get_confirmation_service),
) -> ConfirmResultResponse:
    """Confirm an analysis result. Triggers Smart Diagnosis automatically. Requires MEDTECH role."""
    confirmation = await service.confirm_result(
        result_id=id,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return ConfirmResultResponse.model_validate(confirmation)


@router.post("/{id}/override", response_model=OverrideResponse, status_code=200)
async def override_parameter(
    id: uuid.UUID,
    body: OverrideRequest,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.MEDTECH, UserRole.SUPERVISOR])),
    service: ManualOverrideService = Depends(get_override_service),
) -> OverrideResponse:
    """Override a single AI-generated parameter value.

    `body.original_ai_value` is accepted for API-contract compatibility but
    ignored — the service re-derives the original value from the stored
    `ai_findings` (source of truth), never trusting a client-supplied value.
    """
    override = await service.override_parameter(
        result_id=id,
        parameter=body.parameter,
        corrected_value=body.corrected_value,
        rationale=body.rationale,
        original_ai_value=body.original_ai_value,
        medtech_id=uuid.UUID(current_user["user_id"]),
        request=request,
    )
    return OverrideResponse.model_validate(override)


# ── Supervisor review/approval routes (plan row 7) ─────────────────────────────
# Literal paths first — see module docstring on why order matters here.

@router.get("/supervisor/stats", response_model=SupervisorStatsResponse)
async def get_supervisor_stats(
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> SupervisorStatsResponse:
    return SupervisorStatsResponse(**await service.get_supervisor_stats())


@router.get("/approved-today", response_model=ApprovedTodayListResponse)
async def list_approved_today(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> ApprovedTodayListResponse:
    return ApprovedTodayListResponse(**await service.get_approved_today(page=page, page_size=page_size))


@router.get("/escalated", response_model=EscalatedListResponse)
async def list_escalated(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> EscalatedListResponse:
    return EscalatedListResponse(**await service.get_escalated(page=page, page_size=page_size))


@router.get("/pending", response_model=PendingResultListResponse)
async def list_pending_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> PendingResultListResponse:
    return PendingResultListResponse(**await service.get_pending(page=page, page_size=page_size))


@router.patch("/{result_id}/annotate", response_model=AnnotationResponse)
async def annotate_result(
    result_id: uuid.UUID,
    body: AnnotationRequest,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> AnnotationResponse:
    result = await service.save_annotation(
        result_id=result_id,
        user_id=uuid.UUID(current_user["user_id"]),
        annotation_notes=body.annotation_notes,
        spatial_annotations=body.spatial_annotations,
    )
    return AnnotationResponse(**result)


@router.post("/{result_id}/approve", response_model=ApproveResponse)
async def approve_result(
    result_id: uuid.UUID,
    body: ApproveRequest,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> ApproveResponse:
    result = await service.approve_result(
        result_id=result_id,
        user_id=uuid.UUID(current_user["user_id"]),
        notes=body.notes,
    )
    return ApproveResponse(**result)


@router.post("/{result_id}/return", response_model=ReturnResponse)
async def return_result(
    result_id: uuid.UUID,
    body: ReturnRequest,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> ReturnResponse:
    result = await service.return_result(
        result_id=result_id,
        user_id=uuid.UUID(current_user["user_id"]),
        reason=body.reason,
    )
    return ReturnResponse(**result)


@router.post("/{result_id}/escalate", response_model=EscalateResponse)
async def escalate_result(
    result_id: uuid.UUID,
    body: EscalateRequest,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> EscalateResponse:
    result = await service.escalate_result(
        result_id=result_id,
        user_id=uuid.UUID(current_user["user_id"]),
        escalation_path=body.escalation_path,
        escalation_note=body.escalation_note,
    )
    return EscalateResponse(**result)


@router.get("/{result_id}", response_model=FullResultDetail)
async def get_full_result(
    result_id: uuid.UUID,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> FullResultDetail:
    return FullResultDetail(**await service.get_full_result(result_id))
