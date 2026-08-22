"""Patient-portal authentication: patients log in with their patient UID and
a password derived from their (decrypted) last name + date of birth, rather
than a stored credential.
"""
import asyncio
import unicodedata
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, Request, status

from src.urolens.core import audit_logger
from src.urolens.core.auth_service import (
    close_session,
    create_session,
    increment_failed_attempts,
    reset_failed_attempts,
)
from src.urolens.core.config import settings
from src.urolens.core.encryption import decrypt_pii
from src.urolens.core.supabase import supabase
from src.urolens.schemas.auth import PatientLoginResponse

_PATIENT_TOKEN_EXPIRE_MINUTES = 30


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    # Builds an HTTPException carrying a machine-readable error_code attribute,
    # for endpoints not using one of the typed exceptions in core.exceptions.
    exc = HTTPException(status_code=status_code, detail=message)
    exc.error_code = code  # type: ignore[attr-defined]
    return exc


def _invalid_creds() -> HTTPException:
    # 401 with a generic message — deliberately doesn't distinguish "unknown
    # patient ID" from "wrong password" to avoid leaking which is wrong.
    return _api_error(
        status.HTTP_401_UNAUTHORIZED,
        "INVALID_CREDENTIALS",
        "Patient ID or password is incorrect.",
    )


def _derive_patient_password(last_name: str, date_of_birth: str) -> str:
    # Deterministic password: normalized/uppercased last name + DDMMYYYY DOB.
    # date_of_birth is ISO date string "YYYY-MM-DD" after decryption
    normalized = unicodedata.normalize("NFC", last_name).replace(" ", "").upper()
    dob = datetime.strptime(date_of_birth, "%Y-%m-%d")
    return normalized + dob.strftime("%d%m%Y")


def _issue_patient_jwt(user_id, patient_uid: str, session_id) -> str:
    # Same shape/signing as core.auth_service.issue_jwt, hardcoded to the
    # PATIENT role and a shorter expiry (_PATIENT_TOKEN_EXPIRE_MINUTES).
    now = datetime.now(UTC)
    payload = {
        "user_id": str(user_id),
        "username": patient_uid,
        "role": "PATIENT",
        "session_id": str(session_id),
        "iat": now,
        "exp": now + timedelta(minutes=_PATIENT_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=settings.jwt_algorithm)


async def patient_login(patient_uid: str, password: str, request: Request) -> PatientLoginResponse:
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
    ip_address = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent")

    # 1. Look up patient by patient_uid (plaintext column — safe to query directly)
    patient_result = await supabase.table("patients").select("*").eq(
        "patient_uid", patient_uid
    ).maybe_single().execute()
    patient = patient_result.data

    if patient is None:
        await audit_logger.log_patient_login_failed(ip_address)
        raise _invalid_creds()

    # 2. Get associated user for lockout tracking
    user_result = await supabase.table("users").select("*").eq(
        "user_id", str(patient["user_id"])
    ).maybe_single().execute()
    user = user_result.data

    if user is None:
        await audit_logger.log_patient_login_failed(ip_address, patient_id=patient["patient_id"])
        raise _invalid_creds()

    # 3. Derive and compare password (check before lock to avoid revealing lock status)
    try:
        last_name = decrypt_pii(patient["last_name"])
        date_of_birth = decrypt_pii(patient["date_of_birth"])
        expected = _derive_patient_password(last_name, date_of_birth)
    except Exception:
        await audit_logger.log_patient_login_failed(ip_address, patient_id=patient["patient_id"])
        raise _invalid_creds()

    # Case-insensitive so patients who type lowercase surnames still authenticate
    if password.upper() != expected.upper():
        await asyncio.gather(
            increment_failed_attempts(user["user_id"]),
            audit_logger.log_patient_login_failed(ip_address, patient_id=patient["patient_id"]),
        )
        raise _invalid_creds()

    # 4. Account must not be locked
    if user.get("locked_at") is not None:
        raise _api_error(
            status.HTTP_423_LOCKED,
            "ACCOUNT_LOCKED",
            "Your account is locked. Contact the laboratory.",
        )

    # 5. Account must be active
    if not user.get("is_active", True):
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "ACCOUNT_INACTIVE",
            "Your account is inactive. Contact the laboratory.",
        )

    # 6. Reset failed attempts and create session in parallel
    _, session_record = await asyncio.gather(
        reset_failed_attempts(user["user_id"]),
        create_session(user["user_id"], "PATIENT", ip_address, user_agent),
    )

    token = _issue_patient_jwt(user["user_id"], patient_uid, session_record["session_id"])

    await audit_logger.log_patient_login_success(
        user["user_id"], patient["patient_id"], session_record["session_id"], ip_address
    )

    return PatientLoginResponse(
        access_token=token,
        role="PATIENT",
        user_id=str(user["user_id"]),
    )


async def patient_logout(session_id, user_id, request: Request) -> None:
    """Close a patient's session and record a `PATIENT_LOGOUT` audit entry."""
    ip_address = request.client.host if request.client else "unknown"
    await asyncio.gather(
        close_session(session_id),
        audit_logger.log_patient_logout(user_id, session_id, ip_address),
    )
