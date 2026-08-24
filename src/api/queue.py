"""MedTech queue routes (receptionist-facing): pending specimens, workloads,
and assignment.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from src.core.audit_logger import AuditLogger
from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.core.supabase import getSupabase
from src.schemas.queue import (
    MedTechWorkloadItem,
    PendingSpecimenItem,
    QueueAssignRequest,
    QueueAssignResponse,
)
from src.services.notification_service import NotificationService
from src.services.queue_service import QueueService

router = APIRouter()


async def getQueueService(
    supabaseClient: AsyncClient = Depends(getSupabase),
    sqlalchemyDb: AsyncSession = Depends(getDb),
) -> QueueService:
    """FastAPI dependency constructing a request-scoped `QueueService`."""
    _notificationService = NotificationService(db=sqlalchemyDb)
    return QueueService(
        db=supabaseClient,
        auditLogger=AuditLogger(),
        _notificationService=_notificationService,
        sqlalchemyDb=sqlalchemyDb,
    )


@router.get("/api/v1/queue/pending", response_model=list[PendingSpecimenItem])
async def getPendingSpecimens(
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: QueueService = Depends(getQueueService),
):
    """Return all LABELED specimens awaiting assignment, FIFO order."""
    return await _service.getPendingSpecimens()


@router.get("/api/v1/queue/workloads", response_model=list[MedTechWorkloadItem])
async def getMedtechWorkloads(
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: QueueService = Depends(getQueueService),
):
    """Return all active MedTechs sorted by active queue depth (least-loaded first)."""
    return await _service.getReceptionistWorkloads()


@router.post("/api/v1/queue/assign", response_model=QueueAssignResponse, status_code=201)
async def assignSpecimen(
    data: QueueAssignRequest,
    request: Request,
    currentUser: dict = Depends(RequireRole([UserRole.RECEPTIONIST])),
    _service: QueueService = Depends(getQueueService),
):
    """Assign a LABELED specimen to a MedTech and notify them."""
    return await _service.assignSpecimen(data, currentUser["user_id"], request)
