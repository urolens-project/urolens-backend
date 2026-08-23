"""Patient-portal authentication: patients log in with their patient UID and
a password derived from their (decrypted) last name + date of birth, rather
than a stored credential.
"""
import asyncio
import logging
import unicodedata
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, Request, status

from src.urolens.core import audit_logger
from src.urolens.core.auth_service import (
    closeSession,
    createSession,
    incrementFailedAttempts,
    resetFailedAttempts,
)
from src.urolens.core.config import settings
from src.urolens.core.encryption import decryptPii
from src.urolens.core.supabase import supabase
from src.urolens.schemas.auth import PatientLoginResponse

_PATIENT_TOKEN_EXPIRE_MINUTES = 30
logger = logging.getLogger(__name__)


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


def _derivePatientPassword(lastName: str, dateOfBirth: str) -> str:
    # Deterministic password: normalized/uppercased last name + DDMMYYYY DOB.
    # date_of_birth is ISO date string "YYYY-MM-DD" after decryption
    normalized = unicodedata.normalize("NFC", lastName).replace(" ", "").upper()
    dob = datetime.strptime(dateOfBirth, "%Y-%m-%d")
    return normalized + dob.strftime("%d%m%Y")


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

    Password isn't stored — it's re-derived on each attempt from the
    patient's decrypted last name and date of birth
    (`_derive_patient_password`) and compared case-insensitively. Every
    rejection path is audit-logged.

    Returns:
        A `PatientLoginResponse` with the issued access token.

    Raises:
        HTTPException: 401 (`INVALID_CREDENTIALS`), for an unknown patient
            UID, a user record that can't be resolved, undecryptable PII, or
            a password mismatch. 423 (`ACCOUNT_LOCKED`), if the account is
            locked. 403 (`ACCOUNT_INACTIVE`), if the account is inactive.
    """
    ipAddress = request.client.host if request.client else "unknown"
    userAgent = request.headers.get("user-agent")

    # 1. Look up patient by patient_uid (plaintext column — safe to query directly)
    patientResult = await supabase.table("patients").select("*").eq(
        "patient_uid", patientUid
    ).maybe_single().execute()
    patient = patientResult.data

    if patient is None:
        await audit_logger.logPatientLoginFailed(ipAddress)
        raise _invalidCreds()

    # 2. Get associated user for lockout tracking
    userResult = await supabase.table("users").select("*").eq(
        "user_id", str(patient["user_id"])
    ).maybe_single().execute()
    user = userResult.data

    if user is None:
        await audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"])
        raise _invalidCreds()

    # 3. Derive and compare password (check before lock to avoid revealing lock status)
    try:
        lastName = decryptPii(patient["last_name"])
        dateOfBirth = decryptPii(patient["date_of_birth"])
        expected = _derivePatientPassword(lastName, dateOfBirth)
    except Exception:
        await audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"])
        raise _invalidCreds()

    # Case-insensitive so patients who type lowercase surnames still authenticate
    if password.upper() != expected.upper():
        await asyncio.gather(
            incrementFailedAttempts(user["user_id"]),
            audit_logger.logPatientLoginFailed(ipAddress, patientId=patient["patient_id"]),
        )
        raise _invalidCreds()

    # 4. Account must not be locked
    if user.get("locked_at") is not None:
        raise _apiError(
            status.HTTP_423_LOCKED,
            "ACCOUNT_LOCKED",
            "Your account is locked. Contact the laboratory.",
        )

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
