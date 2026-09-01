"""Result release routes (receptionist-facing): listing approved results
awaiting release and performing the release.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.result_releasing import (
    ApprovedResultsResponse,
    ReleaseResultRequest,
    ResultReleaseResponse,
)
from src.services.notification_service import NotificationService
from src.services.result_releasing_service import ResultReleasingService

router = APIRouter()


async def getResultReleasingService(
    db: AsyncSession = Depends(getDb),
) -> ResultReleasingService:
    """FastAPI dependency constructing a request-scoped `ResultReleasingService`.

    `NotificationService` shares this same `AsyncSession` — both write within
    the one transaction `ResultReleasingService.release_result` commits once,
    so a notification insert can no longer be silently split into a separate,
    unrelated unit of work from the release it belongs to.
    """
    return ResultReleasingService(
        db=db,
        auditLogger=AuditLogger(),
        _notificationService=NotificationService(db=db),
    )


@router.get("/api/v1/results/approved", response_model=ApprovedResultsResponse)
async def getApprovedResults(
    limit: int = 20,
    cursor: str | None = None,
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: ResultReleasingService = Depends(getResultReleasingService),
):
    """List approved results awaiting release; see
    `ResultReleasingService.get_approved_results`.
    """
    return await _service.getApprovedResults(limit=limit, cursor=cursor)


@router.post(
    "/api/v1/results/{result_id}/release",
    response_model=ResultReleaseResponse,
    status_code=201,
)
async def releaseResult(
    result_id: UUID,
    data: ReleaseResultRequest,
    request: Request,
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: ResultReleasingService = Depends(getResultReleasingService),
):
    """Release an approved result; see `ResultReleasingService.release_result`."""
    return await _service.releaseResult(result_id, data.releaseMethod, currentUser, request)
