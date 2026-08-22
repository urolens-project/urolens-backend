"""Result confirm/override (plan row 6), supervisor review/approval (plan
row 7), and Smart Diagnosis lookup (plan: neither row) routes — the full
/api/v1/results surface, in one router since the app/api/results.py ->
src/urolens/api/results.py directory unification folded the last leftover
(GET /{result_id}/smart-diagnosis) in here too. See CHANGELOG.md for the
route-by-route consolidation history.

Route registration order matters here: `GET /pending`, `/approved-today`,
`/escalated`, and `/supervisor/stats` are literal single-segment paths and
must be registered before the catch-all `GET /{result_id}`, or that
catch-all would shadow them. `GET /{result_id}/smart-diagnosis` is a
distinct two-segment shape and isn't at risk of the same collision, but
stays grouped with the other `/{result_id}/...` routes above the catch-all
for readability.

`ConfirmResultResponse`/`OverrideRequest`/`OverrideResponse` used to be
defined inline here rather than in `schemas/`; moved into
`schemas/result_review.py` (see changelog.md's "Inline Pydantic schemas
outside schemas/" entry).
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger, get_audit_logger
from ..core.database import get_db
from ..core.enums import UserRole
from ..core.rbac import RequireRole
from ..schemas.result_review import (
    AnnotationRequest,
    AnnotationResponse,
    ApprovedTodayListResponse,
    ApproveRequest,
    ApproveResponse,
    ConfirmResultResponse,
    EscalatedListResponse,
    EscalateRequest,
    EscalateResponse,
    FullResultDetail,
    OverrideRequest,
    OverrideResponse,
    PendingResultListResponse,
    ReturnRequest,
    ReturnResponse,
    SmartDiagnosisResponse,
    SupervisorStatsResponse,
)
from ..services.manual_override_service import ManualOverrideService
from ..services.notification_service import NotificationService
from ..services.result_confirmation_service import ResultConfirmationService
from ..services.result_review_service import ResultReviewService, get_smart_diagnosis
from ..services.smart_diagnosis_service import SmartDiagnosisService

router = APIRouter(prefix="/api/v1/results", tags=["results"])

_supervisor = RequireRole([UserRole.SUPERVISOR])


# ── Dependency factories (DIP) ────────────────────────────────────────────────

async def get_notif_service(
    db: AsyncSession = Depends(get_db),
) -> NotificationService:
    """FastAPI dependency constructing a request-scoped `NotificationService`."""
    return NotificationService(db=db)


async def get_confirmation_service(
    db: AsyncSession = Depends(get_db),
    audit_logger: AuditLogger = Depends(get_audit_logger),
    notif_service: NotificationService = Depends(get_notif_service),
) -> ResultConfirmationService:
    """FastAPI dependency constructing a request-scoped
    `ResultConfirmationService`, wiring up its `SmartDiagnosisService`
    collaborator.
    """
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
    """FastAPI dependency constructing a request-scoped `ManualOverrideService`."""
    return ManualOverrideService(db=db, audit_logger=audit_logger)


async def get_result_review_service(
    db: AsyncSession = Depends(get_db),
) -> ResultReviewService:
    """FastAPI dependency constructing a request-scoped `ResultReviewService`."""
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
    """Dashboard counts for the supervisor's review queue; see
    `ResultReviewService.get_supervisor_stats`.
    """
    return SupervisorStatsResponse(**await service.get_supervisor_stats())


@router.get("/approved-today", response_model=ApprovedTodayListResponse)
async def list_approved_today(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> ApprovedTodayListResponse:
    """List results approved today; see `ResultReviewService.get_approved_today`."""
    return ApprovedTodayListResponse(**await service.get_approved_today(page=page, page_size=page_size))


@router.get("/escalated", response_model=EscalatedListResponse)
async def list_escalated(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> EscalatedListResponse:
    """List escalated results; see `ResultReviewService.get_escalated`."""
    return EscalatedListResponse(**await service.get_escalated(page=page, page_size=page_size))


@router.get("/pending", response_model=PendingResultListResponse)
async def list_pending_results(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> PendingResultListResponse:
    """List results awaiting supervisor approval; see `ResultReviewService.get_pending`."""
    return PendingResultListResponse(**await service.get_pending(page=page, page_size=page_size))


@router.patch("/{result_id}/annotate", response_model=AnnotationResponse)
async def annotate_result(
    result_id: uuid.UUID,
    body: AnnotationRequest,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> AnnotationResponse:
    """Save a supervisor's annotation on a result; see
    `ResultReviewService.save_annotation`.
    """
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
    """Approve a pending result; see `ResultReviewService.approve_result`."""
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
    """Return a pending result for correction; see `ResultReviewService.return_result`."""
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
    """Escalate a pending result; see `ResultReviewService.escalate_result`."""
    result = await service.escalate_result(
        result_id=result_id,
        user_id=uuid.UUID(current_user["user_id"]),
        escalation_path=body.escalation_path,
        escalation_note=body.escalation_note,
    )
    return EscalateResponse(**result)


@router.get(
    "/{result_id}/smart-diagnosis",
    response_model=SmartDiagnosisResponse,
    summary="Get Smart Diagnosis output for a result",
)
async def get_smart_diagnosis_route(
    result_id: str,
    current_user: dict = Depends(_supervisor),
) -> dict:
    """Fetch a result's Smart Diagnosis output; see
    `result_review_service.get_smart_diagnosis`.
    """
    return await get_smart_diagnosis(result_id=result_id)


@router.get("/{result_id}", response_model=FullResultDetail)
async def get_full_result(
    result_id: uuid.UUID,
    current_user: dict = Depends(_supervisor),
    service: ResultReviewService = Depends(get_result_review_service),
) -> FullResultDetail:
    """Fetch a result's full supervisor-review detail; see
    `ResultReviewService.get_full_result`.
    """
    return FullResultDetail(**await service.get_full_result(result_id))
