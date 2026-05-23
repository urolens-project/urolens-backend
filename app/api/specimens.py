from fastapi import APIRouter, Depends

from app.middleware.rbac import get_current_user
from app.schemas.specimens import RejectSpecimenRequest, RejectSpecimenResponse
from app.services import specimen_service

router = APIRouter(prefix="/api/v1/specimens", tags=["specimens"])


@router.post("/{specimen_id}/reject", response_model=RejectSpecimenResponse)
async def reject_specimen(
    specimen_id: str,
    body: RejectSpecimenRequest,
    claims: dict = Depends(get_current_user),
):
    return await specimen_service.reject_specimen(
        specimen_id=specimen_id,
        user_id=claims["user_id"],
        reason_code=body.reason_code,
        free_text_note=body.free_text_note,
    )
