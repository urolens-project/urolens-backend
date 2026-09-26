"""Specimen labeling routes (receptionist-facing): label search, generation,
and affixed confirmation — part of the receptionist intake pipeline
(receive -> label -> queue assignment), not a MedTech task; a MedTech only
sees a specimen after it's queued to them.
"""
import uuid
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import getDb
from src.core.enums import UserRole
from src.core.rbac import RequireRole
from src.schemas.labeling import (
    LabelConfirmRequest,
    LabelConfirmResponse,
    PrintLabelResponse,
    ReceivedSpecimenSearchItem,
)
from src.services import labeling_service

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Sample Labeling Tracking"]
)

# Matches the frontend's /intake route guard (App.tsx) — the Sample Labeling
# screen there is reachable by Receptionist/Supervisor/Administrator, not
# MedTech. This was previously MEDTECH-only, which 403'd every single call
# the intake labeling screen made, for every role that could ever reach it.
_intake = RequireRole([UserRole.RECEPTIONIST, UserRole.SUPERVISOR, UserRole.ADMINISTRATOR])


@router.get("/search-received", response_model=list[ReceivedSpecimenSearchItem])
async def searchReceivedSpecimens(
    q: str,
    currentUser: dict = Depends(_intake),
    db: AsyncSession = Depends(getDb),
):
    """Search `RECEIVED` specimens by name/UID; see
    `labeling_service.search_received_specimens`.
    """
    return await labeling_service.searchReceivedSpecimens(db, q)


@router.post("/{id}/label", response_model=PrintLabelResponse, status_code=201)
async def generateSpecimenLabelEndpoint(
    id: UUID,
    currentUser: dict = Depends(_intake),
    db: AsyncSession = Depends(getDb),
):
    """Generate a specimen label; see `labeling_service.generate_label`."""
    operatorId = uuid.UUID(currentUser["user_id"])
    return await labeling_service.generateLabel(db, id, operatorId)


@router.post("/{id}/label/confirm", response_model=LabelConfirmResponse)
async def confirmLabelAffixedEndpoint(
    id: UUID,
    currentUser: dict = Depends(_intake),
    db: AsyncSession = Depends(getDb),
    payload: LabelConfirmRequest = Body(...),
):
    """Confirm a label is physically affixed; see
    `labeling_service.confirm_label_affixed`.
    """
    operatorId = uuid.UUID(currentUser["user_id"])
    return await labeling_service.confirmLabelAffixed(
        db, id, operatorId, payload.offlineOverride
    )
