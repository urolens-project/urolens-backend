"""Staff and patient-portal login/logout request/response shapes."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

LoginClient = Literal["web", "mobile"]
"""Which app is logging in. The mobile app is for MedTechs only (UROLENS-244)."""


class LoginRequest(BaseModel):
    """Request body for staff login."""

    username: str = Field(..., min_length=1, max_length=150)
    """Trimmed; blank is refused (UROLENS-165, UROLENS-244)."""
    password: str = Field(..., min_length=1, max_length=256)
    """Never altered — a password's exact characters must reach `verifyPassword`."""
    keepSignedIn: bool = False
    """"Keep me signed in for this shift": the session lasts a whole shift
    (`settings.jwtExpiryHours`) instead of one access-token lifetime."""
    client: LoginClient | None = None
    """`"mobile"` refuses non-MedTech accounts with `ROLE_NOT_ALLOWED`. The web
    may omit it."""

    @field_validator("username")
    @classmethod
    def usernameNotBlank(cls, v: str) -> str:
        """Strip surrounding whitespace; reject a whitespace-only username (UROLENS-165)."""
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("password")
    @classmethod
    def passwordNotBlank(cls, v: str) -> str:
        """Reject a whitespace-only password (UROLENS-165) — `min_length=1`
        alone lets a string of spaces through. Doesn't strip or otherwise
        alter the value: a password's exact characters must reach
        `verifyPassword` unchanged.
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
    expiresAt: datetime
    """When this access token expires; refresh before then to keep working."""
    sessionExpiresAt: datetime
    """When the session ends for good (end of the shift); no refresh goes past it."""


class TokenRefreshResponse(BaseModel):
    """Response body for `POST /auth/refresh` — a new token for the same session."""

    accessToken: str
    tokenType: str = "Bearer"
    expiresAt: datetime
    sessionExpiresAt: datetime


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
