from fastapi import APIRouter, Depends

from app.schemas.results import SmartDiagnosisResponse
from app.services import result_service
from src.urolens.core.rbac import RequireRole

router = APIRouter(prefix="/api/v1/results", tags=["results"])

_supervisor = RequireRole(["SUPERVISOR"])


# ── MedTech endpoints ─────────────────────────────────────────────────────────
# The only route left in this file — see docs/backend-consolidation-plan.md
# row 6/7 notes and CHANGELOG.md: confirm/override moved to
# src/urolens/api/results.py in step 5a, supervisor review/approval moved
# there in step 5b. This route belongs to neither row and stays here.

@router.get(
    "/{result_id}/smart-diagnosis",
    response_model=SmartDiagnosisResponse,
    summary="Get Smart Diagnosis output for a result",
)
async def get_smart_diagnosis(
    result_id: str,
    claims: dict = Depends(_supervisor),
):
    return await result_service.get_smart_diagnosis(result_id=result_id)
