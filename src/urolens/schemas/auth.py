"""Staff and patient-portal login/logout request/response shapes."""
from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Request body for staff login."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Response body for a successful staff login."""

    access_token: str
    token_type: str = "Bearer"
    role: str
    user_id: str


class PatientLoginRequest(BaseModel):
    """Request body for patient-portal login."""

    patient_uid: str
    password: str


class PatientLoginResponse(BaseModel):
    """Response body for a successful patient-portal login."""

    access_token: str
    token_type: str = "Bearer"
    role: str
    user_id: str
