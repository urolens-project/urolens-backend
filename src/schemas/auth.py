"""Staff and patient-portal login/logout request/response shapes."""
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    """Request body for staff login."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)

    @field_validator("username", "password")
    @classmethod
    def notBlank(cls, v: str) -> str:
        """Reject a whitespace-only value (UROLENS-165) — `min_length=1`
        alone lets a string of spaces through. Doesn't strip or otherwise
        alter the value: unlike a free-text note, a password's exact
        characters must reach `verify_password` unchanged.
        """
        if not v.strip():
            raise ValueError("must not be blank")
        return v


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
