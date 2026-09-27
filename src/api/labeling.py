"""Specimen labeling routes (Receptionist-facing): label search, generation,
and affixed confirmation. Was MedTech-gated; corrected to Receptionist per
UROLENS-141's UAC ("Ability for the Receptionist to Generate and Confirm a
Sample Label") — every step here is a Receptionist action, not a MedTech one.
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

_receptionist = RequireRole([UserRole.RECEPTIONIST])


@router.get("/search-received", response_model=list[ReceivedSpecimenSearchItem])
async def searchReceivedSpecimens(
    q: str,
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """Search `RECEIVED` specimens by name/UID; see
    `labeling_service.search_received_specimens`.
    """
    return await labeling_service.searchReceivedSpecimens(db, q)


@router.post("/{id}/label", response_model=PrintLabelResponse, status_code=201)
async def generateSpecimenLabelEndpoint(
    id: UUID,
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """Generate a specimen label; see `labeling_service.generate_label`."""
    operatorId = uuid.UUID(currentUser["user_id"])
    return await labeling_service.generateLabel(db, id, operatorId)


@router.post("/{id}/label/confirm", response_model=LabelConfirmResponse)
async def confirmLabelAffixedEndpoint(
    id: UUID,
    currentUser: dict = Depends(_receptionist),
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
