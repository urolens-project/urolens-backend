import uuid
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.rbac import RequireRole
from src.urolens.core.database import get_db
from src.urolens.core.enums import UserRole
from src.urolens.schemas.specimen import (
    LabRequestSearchItem,
    SpecimenListItem,
    SpecimenReceiveRequest,
    SpecimenReceiveResponse,
    SpecimenRejectRequest,
    SpecimenRejectResponse,
)
from src.urolens.services import lab_request_service, specimen_service

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Specimens Receiving"]
)

_receptionist = RequireRole([UserRole.RECEPTIONIST])
_medtech = RequireRole([UserRole.MEDTECH])


@router.get("", response_model=List[SpecimenListItem])
async def list_specimens_endpoint(
    specimen_status: Optional[str] = Query(default=None, alias="status"),
    current_user: dict = Depends(_receptionist),
    db: AsyncSession = Depends(get_db),
):
    return await specimen_service.list_specimens(db, specimen_status)


@router.get("/search-request", response_model=List[LabRequestSearchItem])
async def search_pending_lab_requests(
    q: str,
    current_user: dict = Depends(_receptionist),
    db: AsyncSession = Depends(get_db),
):
    return await lab_request_service.search_pending_lab_requests(db, q)


@router.post("/receive", response_model=SpecimenReceiveResponse, status_code=201)
async def receive_specimen_endpoint(
    payload: SpecimenReceiveRequest,
    current_user: dict = Depends(_receptionist),
    db: AsyncSession = Depends(get_db),
):
    receptionist_id = uuid.UUID(current_user["user_id"])
    return await specimen_service.receive_specimen(db, receptionist_id, payload)


@router.post("/{specimen_id}/reject", response_model=SpecimenRejectResponse)
async def reject_specimen_endpoint(
    specimen_id: UUID,
    body: SpecimenRejectRequest,
    current_user: dict = Depends(_medtech),
    db: AsyncSession = Depends(get_db),
):
    """
    Ported from app/api/specimens.py + app/services/specimen_service.py
    (consolidation plan row 9 / Track A2 reconciliation). Originally had no
    route-level role gate — ownership was checked inside the service only,
    which the standards skill's rule 2 forbids. Now gated at the route (rule
    2) in addition to the ownership check the service still performs.
    """
    user_id = uuid.UUID(current_user["user_id"])
    return await specimen_service.reject_specimen(
        db, specimen_id, user_id, body.reason_code, body.free_text_note
    )
