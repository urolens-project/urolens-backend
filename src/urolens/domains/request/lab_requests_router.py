"""Lab request routes (receptionist-facing): physician lookup and creation."""
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.core.database import getDb
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


@router.get("/physicians", response_model=list[PhysicianItem])
async def getPhysiciansEndpoint(
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """List active physicians; see `lab_request_service.get_physicians`."""
    return await lab_request_service.getPhysicians(db)


@router.post("", response_model=LabRequestCreateResponse, status_code=201)
async def createLabRequestEndpoint(
    payload: LabRequestCreateRequest,
    request: Request,
    currentUser: dict = Depends(_receptionist),
    db: AsyncSession = Depends(getDb),
):
    """Create a lab request; see `lab_request_service.create_lab_request`."""
    encoderId = uuid.UUID(currentUser["user_id"])
    ipAddress = request.client.host if request.client else None
    return await lab_request_service.createLabRequest(
        db,
        encodedBy=encoderId,
        patientId=payload.patientId,
        testType=payload.testType,
        clinicalNotes=payload.clinicalNotes,
        physicianId=payload.physicianId,
        physicianName=payload.physicianName,
        notifyReceptionists=False,
        ipAddress=ipAddress,
    )
