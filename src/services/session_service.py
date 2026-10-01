"""Renewing a staff session's access token (UROLENS-244); see `api/auth.py`.

An access token lasts `settings.accessTokenExpireMinutes`. While the user is
working, the client swaps it for a fresh one with `POST /auth/refresh`, for the
same session, so an active MedTech isn't signed out mid-task — but never past
`settings.jwtExpiryHours` after login (one shift), when they must log in again.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status

from src.core.auth_service import issueJwt, sessionEndsAt, tokenExpiresAt
from src.core.enums import UserRole
from src.schemas.auth import TokenRefreshResponse


def _apiError(statusCode: int, code: str, message: str) -> HTTPException:
    exc = HTTPException(status_code=statusCode, detail=message)
    exc.errorCode = code  # type: ignore[attr-defined]
    return exc


def refreshAccessToken(claims: dict, now: datetime | None = None) -> TokenRefreshResponse:
    """Issue a new access token for the caller's current session.

    The caller's token has already been verified, and its session found
    active, by `getCurrentUser`. The new token keeps the same session, user,
    role, start time and "keep signed in" choice; it expires one access-token
    lifetime from now, or at the session's end, whichever is sooner.

    Args:
        claims: the verified claims of the caller's current token.
        now: the refresh time; defaults to the current time.

    Raises:
        HTTPException: 403 `ROLE_NOT_ALLOWED`, for a patient-portal token (staff
            sessions only); 401 `SESSION_EXPIRED`, if the session has already
            reached its end.
    """
    if str(claims.get("role", "")).upper() == UserRole.PATIENT:
        raise _apiError(
            status.HTTP_403_FORBIDDEN, "ROLE_NOT_ALLOWED", "Patient sessions can't be refreshed here."
        )
    now = now or datetime.now(UTC)
    # Tokens issued before UROLENS-244 carry no session_start; their issue time
    # is the closest thing to it.
    sessionStart = datetime.fromtimestamp(claims.get("session_start") or claims["iat"], UTC)
    keepSignedIn = bool(claims.get("keep", False))
    sessionEnd = sessionEndsAt(sessionStart)
    if now >= sessionEnd:
        raise _apiError(
            status.HTTP_401_UNAUTHORIZED, "SESSION_EXPIRED", "Your session has expired. Please log in again."
        )
    token = issueJwt(
        claims["user_id"],
        claims.get("username", ""),
        claims["role"],
        claims["session_id"],
        sessionStart=sessionStart,
        keepSignedIn=keepSignedIn,
        now=now,
    )
    return TokenRefreshResponse(
        accessToken=token,
        expiresAt=tokenExpiresAt(sessionStart, keepSignedIn, now),
        sessionExpiresAt=sessionEnd,
    )
