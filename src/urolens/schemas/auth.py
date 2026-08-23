"""Staff and patient-portal login/logout request/response shapes."""
from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Request body for staff login."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Response body for a successful staff login."""

    accessToken: str
    tokenType: str = "Bearer"
    role: str
    userId: str


class PatientLoginRequest(BaseModel):
    """Request body for patient-portal login."""

    patientUid: str
    password: str


class PatientLoginResponse(BaseModel):
    """Response body for a successful patient-portal login."""

    accessToken: str
    tokenType: str = "Bearer"
    role: str
    userId: str
