"""Patient-portal authentication: patients log in with their patient UID and
the bcrypt-hashed password set at intake (see `PatientService._generateOtpPassword`),
verified the same way staff credentials are (`api.auth.login` / `core.auth_service.verifyPassword`).
"""
import asyncio
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, Request, status

from src.core import audit_logger
from src.core.auth_service import (
    LOCKOUT_MINUTES,
    closeSession,
    createSession,
    incrementFailedAttempts,
    isLockedOut,
    resetFailedAttempts,
    spendPasswordCheck,
    verifyPassword,
)
from src.core.config import settings
from src.core.rate_limit import clearLoginRateLimit, enforceLoginRateLimit
from src.core.supabase import supabase
from src.schemas.auth import PatientLoginResponse

_PATIENT_TOKEN_EXPIRE_MINUTES = 30


def _apiError(statusCode: int, code: str, message: str) -> HTTPException:
    # Builds an HTTPException carrying a machine-readable error_code attribute,
    # for endpoints not using one of the typed exceptions in core.exceptions.
    exc = HTTPException(status_code=statusCode, detail=message)
    exc.errorCode = code  # type: ignore[attr-defined]
    return exc


def _invalidCreds() -> HTTPException:
    # 401 with a generic message — deliberately doesn't distinguish "unknown
    # patient ID" from "wrong password" to avoid leaking which is wrong.
    return _apiError(
        status.HTTP_401_UNAUTHORIZED,
        "INVALID_CREDENTIALS",
        "Patient ID or password is incorrect.",
    )


def _issuePatientJwt(userId, patientUid: str, sessionId) -> str:
    # Same shape/signing as core.auth_service.issue_jwt, hardcoded to the
    # PATIENT role and a shorter expiry (_PATIENT_TOKEN_EXPIRE_MINUTES).
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "username": patientUid,
        "role": "PATIENT",
        "session_id": str(sessionId),
        "iat": now,
        "exp": now + timedelta(minutes=_PATIENT_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


async def patientLogin(patientUid: str, password: str, request: Request) -> PatientLoginResponse:
    """Authenticate a patient-portal login and issue a scoped access token.

    Checks the bcrypt hash stored on the linked `users` row (the same
    `verifyPassword` staff login uses) — not a value re-derived from PII.
    Every rejection path is audit-logged.

    Returns:
        A `PatientLoginResponse` with the issued access token.

    Raises:
        HTTPException: 401 (`INVALID_CREDENTIALS`), for an unknown patient
            UID, a user record that can't be resolved, or a password
            mismatch (every such path takes one bcrypt check, so timing
            doesn't reveal which patient UIDs exist). 423 (`ACCOUNT_LOCKED`),
            inside the `LOCKOUT_MINUTES` window after too many failures.
            403 (`ACCOUNT_INACTIVE`), if the account is inactive.
        TooManyRequestsException: 429 (`TOO_MANY_LOGIN_ATTEMPTS`), if this
            patient UID or client IP is over its login rate limit.
    """
    ipAddress = request.client.host if request.client else "unknown"
    userAgent = request.headers.get("user-agent")

    # 0. Rate limit before any lookup or password check (UROLENS-222, F-06)
    enforceLoginRateLimit("patient", patientUid, ipAddress)

    # 1. Look up patient by patient_uid (plaintext column — safe to query directly)
    patientResult = await supabase.table("patients").select("*").eq(
        "patient_uid", patientUid
    ).maybe_single().execute()
    patient = patientResult.data

    if patient is None:
        await spendPasswordCheck(password)  # same timing as a wrong password
        await audit_logger.logPatientLoginFailed(ipAddress)
        raise _invalidCreds()

    # 2. Get associated user for lockout tracking
    userResult = await supabase.table("users").select("*").eq(
        "user_id", str(patient["user_id"])
    ).maybe_single().execute()
    user = userResult.data

    if user is None:
        await spendPasswordCheck(password)
        await audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"])
        raise _invalidCreds()

    # 3. The password is always checked (same timing either way)...
    passwordOk = await verifyPassword(password, user["hashed_password"])

    # 4. ...but while locked, the answer is 423 whether it was right or wrong,
    # and nothing is counted: a wrong guess can't extend the lock, and the
    # response can't tell an attacker their guess was right (UROLENS-222, F-07).
    if isLockedOut(user):
        if not passwordOk:
            await audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"])
        raise _apiError(
            status.HTTP_423_LOCKED,
            "ACCOUNT_LOCKED",
            f"Your account is temporarily locked. Try again in {LOCKOUT_MINUTES} minutes or contact the laboratory.",
        )

    if not passwordOk:
        await asyncio.gather(
            incrementFailedAttempts(user["user_id"]),
            audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"]),
        )
        raise _invalidCreds()

    # 5. Account must be active
    if not user.get("is_active", True):
        raise _apiError(
            status.HTTP_403_FORBIDDEN,
            "ACCOUNT_INACTIVE",
            "Your account is inactive. Contact the laboratory.",
        )

    # 6. Reset failed attempts and create session in parallel
    _, sessionRecord = await asyncio.gather(
        resetFailedAttempts(user["user_id"]),
        createSession(user["user_id"], "PATIENT", ipAddress, userAgent),
    )

    clearLoginRateLimit("patient", patientUid)
    token = _issuePatientJwt(user["user_id"], patientUid, sessionRecord["session_id"])

    await audit_logger.logPatientLoginSuccess(
        user["user_id"], patient["patient_id"], sessionRecord["session_id"], ipAddress
    )

    return PatientLoginResponse(
        accessToken=token,
        role="PATIENT",
        userId=str(user["user_id"]),
    )


async def patientLogout(sessionId, userId, request: Request) -> None:
    """Close a patient's session and record a `PATIENT_LOGOUT` audit entry."""
    ipAddress = request.client.host if request.client else "unknown"
    await asyncio.gather(
        closeSession(sessionId),
        audit_logger.logPatientLogout(userId, sessionId, ipAddress),
    )
