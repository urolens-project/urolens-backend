"""Lab request routes (receptionist-facing): physician lookup and creation."""
import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.database import get_db
from src.urolens.core.enums import UserRole
from src.urolens.core.rbac import RequireRole
from src.urolens.schemas.lab_request import (
    LabRequestCreateRequest,
    LabRequestCreateResponse,
    PhysicianItem,
)
from src.urolens.services import lab_request_service

router = APIRouter(
    prefix="/api/v1/lab-requests",
    tags=["Lab Requests"]
)

_receptionist = RequireRole([UserRole.RECEPTIONIST])


@router.get("/physicians", response_model=List[PhysicianItem])
async def get_physicians_endpoint(
    current_user: dict = Depends(_receptionist),
    db: AsyncSession = Depends(get_db),
):
    """List active physicians; see `lab_request_service.get_physicians`."""
    return await lab_request_service.get_physicians(db)


@router.post("", response_model=LabRequestCreateResponse, status_code=201)
async def create_lab_request_endpoint(
    payload: LabRequestCreateRequest,
    current_user: dict = Depends(_receptionist),
    db: AsyncSession = Depends(get_db),
):
    """Create a lab request; see `lab_request_service.create_lab_request`."""
    encoder_id = uuid.UUID(current_user["user_id"])
    return await lab_request_service.create_lab_request(db, encoder_id, payload)
