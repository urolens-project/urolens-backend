"""Specimen labeling routes (MedTech-facing): label search, generation, and
affixed confirmation.
"""
import uuid
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.database import get_db
from src.urolens.core.enums import UserRole
from src.urolens.core.rbac import RequireRole
from src.urolens.schemas.labeling import (
    LabelConfirmRequest,
    LabelConfirmResponse,
    PrintLabelResponse,
    ReceivedSpecimenSearchItem,
)
from src.urolens.services import labeling_service

router = APIRouter(
    prefix="/api/v1/specimens",
    tags=["Sample Labeling Tracking"]
)

_medtech = RequireRole([UserRole.MEDTECH])


@router.get("/search-received", response_model=list[ReceivedSpecimenSearchItem])
async def search_received_specimens(
    q: str,
    current_user: dict = Depends(_medtech),
    db: AsyncSession = Depends(get_db),
):
    """Search `RECEIVED` specimens by name/UID; see
    `labeling_service.search_received_specimens`.
    """
    return await labeling_service.search_received_specimens(db, q)


@router.post("/{id}/label", response_model=PrintLabelResponse, status_code=201)
async def generate_specimen_label_endpoint(
    id: UUID,
    current_user: dict = Depends(_medtech),
    db: AsyncSession = Depends(get_db),
):
    """Generate a specimen label; see `labeling_service.generate_label`."""
    operator_id = uuid.UUID(current_user["user_id"])
    return await labeling_service.generate_label(db, id, operator_id)


@router.post("/{id}/label/confirm", response_model=LabelConfirmResponse)
async def confirm_label_affixed_endpoint(
    id: UUID,
    current_user: dict = Depends(_medtech),
    db: AsyncSession = Depends(get_db),
    payload: LabelConfirmRequest = Body(...),
):
    """Confirm a label is physically affixed; see
    `labeling_service.confirm_label_affixed`.
    """
    operator_id = uuid.UUID(current_user["user_id"])
    return await labeling_service.confirm_label_affixed(
        db, id, operator_id, payload.offline_override
    )
