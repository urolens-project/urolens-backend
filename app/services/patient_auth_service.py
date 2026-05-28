import asyncio
import unicodedata
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request, status

from app.config import JWT_ALGORITHM, JWT_SIGNING_KEY
from app.db.supabase import supabase
from app.services import audit_logger
from app.services.auth_service import (
    close_session,
    create_session,
    increment_failed_attempts,
    reset_failed_attempts,
)
from app.schemas.auth import PatientLoginResponse
from src.urolens.core.encryption import decrypt_pii

_PATIENT_TOKEN_EXPIRE_MINUTES = 30


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    exc = HTTPException(status_code=status_code, detail=message)
    exc.error_code = code  # type: ignore[attr-defined]
    return exc


def _invalid_creds() -> HTTPException:
    return _api_error(
        status.HTTP_401_UNAUTHORIZED,
        "INVALID_CREDENTIALS",
        "Patient ID or password is incorrect.",
    )


def _derive_patient_password(last_name: str, date_of_birth: str) -> str:
    # date_of_birth is ISO date string "YYYY-MM-DD" after decryption
    normalized = unicodedata.normalize("NFC", last_name).replace(" ", "").upper()
    dob = datetime.strptime(date_of_birth, "%Y-%m-%d")
    return normalized + dob.strftime("%d%m%Y")


def _issue_patient_jwt(user_id, patient_uid: str, session_id) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": str(user_id),
        "username": patient_uid,
        "role": "PATIENT",
        "session_id": str(session_id),
        "iat": now,
        "exp": now + timedelta(minutes=_PATIENT_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SIGNING_KEY, algorithm=JWT_ALGORITHM)


async def patient_login(patient_uid: str, password: str, request: Request) -> PatientLoginResponse:
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
        from app.config import ENCRYPTION_KEY
        print(f"[DEBUG] key_prefix={ENCRYPTION_KEY[:8]!r} key_len={len(ENCRYPTION_KEY)}")  # REMOVE
        last_name = decrypt_pii(patient["last_name"])
        date_of_birth = decrypt_pii(patient["date_of_birth"])
        expected = _derive_patient_password(last_name, date_of_birth)
    except Exception as e:
        print(f"[DEBUG] step=decrypt error={type(e).__name__}")  # REMOVE
        await audit_logger.log_patient_login_failed(ip_address, patient_id=patient["patient_id"])
        raise _invalid_creds()

    print(f"[DEBUG] step=compare expected={expected!r} received={password!r}")  # REMOVE
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
    ip_address = request.client.host if request.client else "unknown"
    await asyncio.gather(
        close_session(session_id),
        audit_logger.log_patient_logout(user_id, session_id, ip_address),
    )
