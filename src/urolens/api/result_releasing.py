"""Result release routes (receptionist-facing): listing approved results
awaiting release and performing the release."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from supabase import AsyncClient

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.enums import UserRole
from src.urolens.core.rbac import RequireRole
from src.urolens.core.supabase import get_supabase
from src.urolens.schemas.result_releasing import (
    ApprovedResultsResponse,
    ReleaseResultRequest,
    ResultReleaseResponse,
)
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.result_releasing_service import ResultReleasingService

router = APIRouter()


async def get_result_releasing_service(
    db: AsyncClient = Depends(get_supabase),
) -> ResultReleasingService:
    """FastAPI dependency constructing a request-scoped `ResultReleasingService`."""
    return ResultReleasingService(
        db=db,
        audit_logger=AuditLogger(),
        notification_service=NotificationService(db=db),
    )


@router.get("/api/v1/results/approved", response_model=ApprovedResultsResponse)
async def get_approved_results(
    limit: int = 20,
    cursor: str | None = None,
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: ResultReleasingService = Depends(get_result_releasing_service),
):
    """List approved results awaiting release; see
    `ResultReleasingService.get_approved_results`."""
    return await service.get_approved_results(limit=limit, cursor=cursor)


@router.post(
    "/api/v1/results/{result_id}/release",
    response_model=ResultReleaseResponse,
    status_code=201,
)
async def release_result(
    result_id: UUID,
    data: ReleaseResultRequest,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: ResultReleasingService = Depends(get_result_releasing_service),
):
    """Release an approved result; see `ResultReleasingService.release_result`."""
    return await service.release_result(result_id, data.release_method, current_user, request)
