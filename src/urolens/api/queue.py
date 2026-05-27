from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from app.db.supabase import get_supabase
from app.middleware.rbac import RequireRole
from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.database import get_db
from src.urolens.core.enums import UserRole
from src.urolens.schemas.queue import MedTechWorkload, QueueAssignRequest, QueueAssignResponse
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.queue_service import QueueService

router = APIRouter()


async def get_queue_service(
    supabase_client: AsyncClient = Depends(get_supabase),
    sqlalchemy_db: AsyncSession = Depends(get_db),
) -> QueueService:
    notification_service = NotificationService(db=sqlalchemy_db)
    return QueueService(
        db=supabase_client,
        audit_logger=AuditLogger(),
        notification_service=notification_service,
        sqlalchemy_db=sqlalchemy_db,
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
