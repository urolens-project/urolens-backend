import json
from datetime import datetime, timezone

from app.config import ZERO_UUID
from app.db.supabase import supabase


async def _create_audit_entry(
    event_type: str,
    ip_address: str,
    user_id=None,
    session_id=None,
    detail: dict | None = None,
) -> None:
    if detail is None:
        detail = {}
    if session_id:
        detail["session_id"] = str(session_id)

    entry = {
        "event_type": event_type,
        "entity_type": "auth",
        "entity_id": str(user_id) if user_id else ZERO_UUID,
        "user_id": str(user_id) if user_id else None,
        "ip_address": ip_address,
        "detail_json": json.dumps(detail) if detail else None,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    await supabase.table("audit_logs").insert(entry).execute()


async def log_login_success(user_id, session_id, ip_address: str) -> None:
    await _create_audit_entry(
        "LOGIN_SUCCESS",
        ip_address,
        user_id=user_id,
        session_id=session_id,
    )


async def log_login_failed(ip_address: str, user_id=None) -> None:
    detail = {"reason": "invalid_password" if user_id else "user_not_found"}
    await _create_audit_entry(
        "LOGIN_FAILED",
        ip_address,
        user_id=user_id,
        detail=detail,
    )


async def log_logout(user_id, session_id, ip_address: str) -> None:
    await _create_audit_entry(
        "LOGOUT",
        ip_address,
        user_id=user_id,
        session_id=session_id,
    )


async def log_access_denied(ip_address: str, user_id=None) -> None:
    await _create_audit_entry(
        "ACCESS_DENIED",
        ip_address,
        user_id=user_id,
    )


async def log_patient_login_success(
    user_id, patient_id, session_id, ip_address: str
) -> None:
    await _create_audit_entry(
        "PATIENT_LOGIN",
        ip_address,
        user_id=user_id,
        session_id=session_id,
        detail={"role": "PATIENT", "patient_id": str(patient_id)},
    )


async def log_patient_login_failed(ip_address: str, patient_id=None) -> None:
    detail: dict = {}
    if patient_id:
        detail["patient_id"] = str(patient_id)
    await _create_audit_entry(
        "PATIENT_LOGIN_FAILED",
        ip_address,
        detail=detail if detail else None,
    )


async def log_patient_logout(user_id, session_id, ip_address: str) -> None:
    await _create_audit_entry(
        "PATIENT_LOGOUT",
        ip_address,
        user_id=user_id,
        session_id=session_id,
    )
