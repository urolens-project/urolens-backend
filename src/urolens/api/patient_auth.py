"""Patient-portal login/logout routes."""
from fastapi import APIRouter, Depends, Request, Response, status

from src.urolens.core.rbac import get_current_user
from src.urolens.schemas.auth import PatientLoginRequest, PatientLoginResponse
from src.urolens.services.patient_auth_service import patient_login, patient_logout

router = APIRouter(prefix="/api/v1/auth", tags=["patient-auth"])


@router.post("/patient-login", response_model=PatientLoginResponse, status_code=status.HTTP_200_OK)
async def login_patient(body: PatientLoginRequest, request: Request):
    """Authenticate a patient-portal login; see `patient_auth_service.patient_login`."""
    return await patient_login(body.patient_uid, body.password, request)


@router.post("/patient-logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_patient(request: Request, claims: dict = Depends(get_current_user)):
    """Log out the authenticated patient's session."""
    await patient_logout(claims["session_id"], claims["user_id"], request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
