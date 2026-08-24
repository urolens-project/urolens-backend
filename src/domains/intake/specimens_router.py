"""Specimen receiving routes (receptionist-facing) plus post-assignment
MedTech rejection.
"""
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.specimen import (
    LabRequestSearchItem,
    SpecimenListItem,
    SpecimenReceiveRequest,
    SpecimenReceiveResponse,
    SpecimenRejectRequest,
    SpecimenRejectResponse,
)
from src.services import lab_request_service, specimen_service

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Specimens Receiving"]
)

_receptionist = RequireRole([UserRole.RECEPTIONIST])
_medtech = RequireRole([UserRole.MEDTECH])


@router.get("", response_model=list[SpecimenListItem])
async def listSpecimensEndpoint(
    specimenStatus: str | None = Query(default=None, alias="status"),
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """List specimens, optionally filtered by status; see
    `specimen_service.list_specimens`.
    """
    return await specimen_service.listSpecimens(db, specimenStatus)


@router.get("/search-request", response_model=list[LabRequestSearchItem])
async def searchPendingLabRequests(
    q: str,
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """Search `PENDING_SAMPLE` lab requests; see
    `lab_request_service.search_pending_lab_requests`.
    """
    return await lab_request_service.searchPendingLabRequests(db, q)


@router.post("/receive", response_model=SpecimenReceiveResponse, status_code=201)
async def receiveSpecimenEndpoint(
    payload: SpecimenReceiveRequest,
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """Receive a specimen against a lab request; see
    `specimen_service.receive_specimen`.
    """
    receptionistId = uuid.UUID(currentUser["user_id"])
    return await specimen_service.receiveSpecimen(db, receptionistId, payload)


@router.post("/{specimen_id}/reject", response_model=SpecimenRejectResponse)
async def rejectSpecimenEndpoint(
    specimen_id: UUID,
    body: SpecimenRejectRequest,
    currentUser: dict = Depends(_medtech),
    db: AsyncSession = Depends(getDb),
):
    """Ported from app/api/specimens.py + app/services/specimen_service.py
    (consolidation plan row 9 / Track A2 reconciliation). Originally had no
    route-level role gate — ownership was checked inside the service only,
    which the standards skill's rule 2 forbids. Now gated at the route (rule
    2) in addition to the ownership check the service still performs.
    """
    userId = uuid.UUID(currentUser["user_id"])
    return await specimen_service.rejectSpecimen(
        db, specimen_id, userId, body.reasonCode, body.freeTextNote
    )
