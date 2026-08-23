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

from ..core.audit_logger import AuditLogger, getAuditLogger
from ..core.database import getDb
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
from ..services.result_review_service import ResultReviewService, getSmartDiagnosis
from ..services.smart_diagnosis_service import SmartDiagnosisService

router = APIRouter(prefix="/api/v1/results", tags=["results"])

_REQUIRE_SUPERVISOR = RequireRole([UserRole.SUPERVISOR])
_REQUIRE_MEDTECH = RequireRole([UserRole.MEDTECH])
_REQUIRE_BOTH = RequireRole([UserRole.MEDTECH, UserRole.SUPERVISOR])

# ── Dependency factories (DIP) ────────────────────────────────────────────────

async def getNotifService(
    db: AsyncSession = Depends(getDb),
) -> NotificationService:
    """FastAPI dependency constructing a request-scoped `NotificationService`."""
    return NotificationService(db=db)


async def getConfirmationService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
    _notifService: NotificationService = Depends(getNotifService),
) -> ResultConfirmationService:
    """FastAPI dependency constructing a request-scoped
    `ResultConfirmationService`, wiring up its `SmartDiagnosisService`
    collaborator.
    """
    _smartDiag = SmartDiagnosisService(
        auditLogger=auditLogger,
        _notifService=_notifService,
    )
    return ResultConfirmationService(
        db=db,
        auditLogger=auditLogger,
        _smartDiagnosisService=_smartDiag,
        _notifService=_notifService,
    )


async def getOverrideService(
    db: AsyncSession = Depends(getDb),
    auditLogger: AuditLogger = Depends(getAuditLogger),
) -> ManualOverrideService:
    """FastAPI dependency constructing a request-scoped `ManualOverrideService`."""
    return ManualOverrideService(db=db, auditLogger=auditLogger)


async def getResultReviewService(
    db: AsyncSession = Depends(getDb),
) -> ResultReviewService:
    """FastAPI dependency constructing a request-scoped `ResultReviewService`."""
    return ResultReviewService(db=db)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/{id}/confirm", response_model=ConfirmResultResponse, status_code=200)
async def confirmResult(
    id: uuid.UUID,
    request: Request,
    currentUser: dict = Depends(_REQUIRE_MEDTECH)),
    _service: ResultConfirmationService = Depends(getConfirmationService),
) -> ConfirmResultResponse:
    """Confirm an analysis result. Triggers Smart Diagnosis automatically. Requires MEDTECH role."""
    confirmation = await _service.confirmResult(
        resultId=id,
        medtechId=uuid.UUID(currentUser["user_id"]),
        request=request,
    )
    return ConfirmResultResponse.model_validate(confirmation)


@router.post("/{id}/override", response_model=OverrideResponse, status_code=200)
async def overrideParameter(
    id: uuid.UUID,
    body: OverrideRequest,
    request: Request,
    currentUser: dict = Depends(_REQUIRE_BOTH),
    _service: ManualOverrideService = Depends(getOverrideService),
) -> OverrideResponse:
    """Override a single AI-generated parameter value.

    `body.original_ai_value` is accepted for API-contract compatibility but
    ignored — the service re-derives the original value from the stored
    `ai_findings` (source of truth), never trusting a client-supplied value.
    """
    override = await _service.overrideParameter(
        resultId=id,
        parameter=body.parameter,
        correctedValue=body.correctedValue,
        rationale=body.rationale,
        originalAiValue=body.originalAiValue,
        medtechId=uuid.UUID(currentUser["user_id"]),
        request=request,
    )
    return OverrideResponse.model_validate(override)


# ── Supervisor review/approval routes (plan row 7) ─────────────────────────────
# Literal paths first — see module docstring on why order matters here.

@router.get("/supervisor/stats", response_model=SupervisorStatsResponse)
async def getSupervisorStats(
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> SupervisorStatsResponse:
    """Dashboard counts for the supervisor's review queue; see
    `ResultReviewService.get_supervisor_stats`.
    """
    return SupervisorStatsResponse(**await _service.getSupervisorStats())


@router.get("/approved-today", response_model=ApprovedTodayListResponse)
async def listApprovedToday(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> ApprovedTodayListResponse:
    """List results approved today; see `ResultReviewService.get_approved_today`."""
    return ApprovedTodayListResponse(**await _service.getApprovedToday(page=page, pageSize=pageSize))


@router.get("/escalated", response_model=EscalatedListResponse)
async def listEscalated(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> EscalatedListResponse:
    """List escalated results; see `ResultReviewService.get_escalated`."""
    return EscalatedListResponse(**await _service.getEscalated(page=page, pageSize=pageSize))


@router.get("/pending", response_model=PendingResultListResponse)
async def listPendingResults(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> PendingResultListResponse:
    """List results awaiting supervisor approval; see `ResultReviewService.get_pending`."""
    return PendingResultListResponse(**await _service.getPending(page=page, pageSize=pageSize))


@router.patch("/{result_id}/annotate", response_model=AnnotationResponse)
async def annotateResult(
    result_id: uuid.UUID,
    body: AnnotationRequest,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> AnnotationResponse:
    """Save a supervisor's annotation on a result; see
    `ResultReviewService.save_annotation`.
    """
    result = await _service.saveAnnotation(
        resultId=result_id,
        userId=uuid.UUID(currentUser["user_id"]),
        annotationNotes=body.annotationNotes,
        spatialAnnotations=body.spatialAnnotations,
    )
    return AnnotationResponse(**result)


@router.post("/{result_id}/approve", response_model=ApproveResponse)
async def approveResult(
    result_id: uuid.UUID,
    body: ApproveRequest,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> ApproveResponse:
    """Approve a pending result; see `ResultReviewService.approve_result`."""
    result = await _service.approveResult(
        resultId=result_id,
        userId=uuid.UUID(currentUser["user_id"]),
        notes=body.notes,
    )
    return ApproveResponse(**result)


@router.post("/{result_id}/return", response_model=ReturnResponse)
async def returnResult(
    result_id: uuid.UUID,
    body: ReturnRequest,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> ReturnResponse:
    """Return a pending result for correction; see `ResultReviewService.return_result`."""
    result = await _service.returnResult(
        resultId=result_id,
        userId=uuid.UUID(currentUser["user_id"]),
        reason=body.reason,
    )
    return ReturnResponse(**result)


@router.post("/{result_id}/escalate", response_model=EscalateResponse)
async def escalateResult(
    result_id: uuid.UUID,
    body: EscalateRequest,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> EscalateResponse:
    """Escalate a pending result; see `ResultReviewService.escalate_result`."""
    result = await _service.escalateResult(
        resultId=result_id,
        userId=uuid.UUID(currentUser["user_id"]),
        escalationPath=body.escalationPath,
        escalationNote=body.escalationNote,
    )
    return EscalateResponse(**result)


@router.get(
    "/{result_id}/smart-diagnosis",
    response_model=SmartDiagnosisResponse,
    summary="Get Smart Diagnosis output for a result",
)
async def getSmartDiagnosisRoute(
    result_id: str,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
) -> dict:
    """Fetch a result's Smart Diagnosis output; see
    `result_review_service.get_smart_diagnosis`.
    """
    return await getSmartDiagnosis(resultId=result_id)


@router.get("/{result_id}", response_model=FullResultDetail)
async def getFullResult(
    result_id: uuid.UUID,
    currentUser: dict = Depends(_REQUIRE_SUPERVISOR),
    _service: ResultReviewService = Depends(getResultReviewService),
) -> FullResultDetail:
    """Fetch a result's full supervisor-review detail; see
    `ResultReviewService.get_full_result`.
    """
    return FullResultDetail(**await _service.getFullResult(result_id))
