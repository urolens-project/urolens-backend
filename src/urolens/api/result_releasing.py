"""Result release routes (receptionist-facing): listing approved results
awaiting release and performing the release.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.database import getDb
from src.urolens.core.enums import UserRole
from src.urolens.core.rbac import RequireRole
from src.urolens.core.supabase import getSupabase
from src.urolens.schemas.result_releasing import (
    ApprovedResultsResponse,
    ReleaseResultRequest,
    ResultReleaseResponse,
)
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.result_releasing_service import ResultReleasingService

router = APIRouter()


async def getResultReleasingService(
    db: AsyncClient = Depends(getSupabase),
    sqlalchemyDb: AsyncSession = Depends(getDb),
) -> ResultReleasingService:
    """FastAPI dependency constructing a request-scoped `ResultReleasingService`.

    `NotificationService` needs a real SQLAlchemy `AsyncSession` (it writes
    notification rows via SQLAlchemy Core) — it must not be constructed with
    the Supabase `AsyncClient` `db` still used for everything else in this
    service, which silently broke notification delivery on every digital
    result release (the insert failed and was swallowed by
    `NotificationService.notify`'s own best-effort try/except).
    """
    return ResultReleasingService(
        db=db,
        auditLogger=AuditLogger(),
        _notificationService=NotificationService(db=sqlalchemyDb),
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
