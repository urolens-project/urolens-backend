from fastapi import APIRouter, Depends, Request
from supabase import AsyncClient

from app.db.supabase import get_supabase
from app.middleware.rbac import RequireRole
from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.enums import UserRole
from src.urolens.schemas.queue import MedTechWorkload, QueueAssignRequest, QueueAssignResponse
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.queue_service import QueueService

router = APIRouter()


async def get_queue_service(
    db: AsyncClient = Depends(get_supabase),
) -> QueueService:
    notification_service = NotificationService(db=db)
    return QueueService(
        db=db,
        audit_logger=AuditLogger(db=db),
        notification_service=notification_service,
    )


@router.get("/api/v1/queue/pending", response_model=list[MedTechWorkload])
async def get_pending_queue(
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: QueueService = Depends(get_queue_service),
):
    return await service.get_workloads()


@router.post("/api/v1/queue/assign", response_model=QueueAssignResponse, status_code=201)
async def assign_specimen(
    data: QueueAssignRequest,
    request: Request,
    current_user: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    service: QueueService = Depends(get_queue_service),
):
    return await service.assign_specimen(data, current_user["user_id"], request)
