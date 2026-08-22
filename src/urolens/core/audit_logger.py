"""Audit-log writing: the single `audit_logs`-table code path (`AuditLogger.record`)
plus a set of auth-flow convenience wrappers around it (login/logout/access-denied
events)."""
from __future__ import annotations

import uuid
from typing import Any

from .config import settings
from .supabase import supabase


class AuditLogger:
    """Writes to the shared `audit_logs` table. The one audit-writing code
    path in this app — the auth-flow helpers below are thin wrappers around
    `record()`, not a second path (they were, until this consolidation)."""

    async def record(
        self,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | str,
        user_id: uuid.UUID | str | None,
        db: Any = None,          # kept for backward compatibility, ignored
        detail_json: dict[str, Any] | None = None,
        request: Any = None,
        ip_address: str | None = None,
    ) -> None:
        """Insert one audit_logs row. Never raises — a failure to write the
        audit entry must not break whatever the caller was actually doing.

        Args:
            db: unused, kept only so existing call sites that still pass a
                SQLAlchemy session don't need updating.
            request: if given and `ip_address` isn't, the client IP is read
                from `request.client.host`.
            ip_address: takes precedence over `request` — for callers that
                already have a raw IP string rather than a Request object.
        """
        if ip_address is None and request and hasattr(request, "client") and request.client:
            ip_address = request.client.host

        try:
            await supabase.table("audit_logs").insert({
                "log_id": str(uuid.uuid4()),
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "user_id": str(user_id) if user_id else None,
                "detail_json": detail_json or {},
                "ip_address": ip_address,
            }).execute()
        except Exception:
            # Audit must never break the main transaction
            pass


def get_audit_logger() -> AuditLogger:
    """Return a new `AuditLogger` instance.

    Returns:
        A fresh `AuditLogger`. The class holds no state, so this is
        equivalent to constructing one directly — provided as a FastAPI
        dependency-friendly factory.
    """
    return AuditLogger()


# ── Auth-flow helpers ───────────────────────────────────────────────────────
# Reconciled from the former app/services/audit_logger.py — a second,
# auth-specific audit code path that wrote the same audit_logs table
# directly via its own Supabase call, never sharing logic with AuditLogger
# above despite writing the same table. Now thin wrappers around
# AuditLogger.record(), one code path instead of two.
#
# Two deliberate behavior changes from the pre-reconciliation version:
#   - detail_json is passed as a raw dict, matching every other JSONB write
#     in this codebase — the old version json.dumps()'d it into a string
#     first, which would have stored a JSON-encoded string inside a JSONB
#     column instead of a structured object.
#   - a failure to write the audit entry itself is now swallowed (via
#     record()'s try/except), not propagated. Propagating meant an audit-
#     logging glitch could turn an intended 401 (see get_current_user in
#     core/rbac.py, which calls log_access_denied before raising) into an
#     unrelated 500 — strictly worse, and inconsistent with record()'s own
#     "audit must never break the main transaction" design that every other
#     caller in this app already relies on.

_audit_logger = AuditLogger()


async def log_login_success(user_id, session_id, ip_address: str) -> None:
    """Record a successful staff login as a `LOGIN_SUCCESS` audit entry."""
    await _audit_logger.record(
        event_type="LOGIN_SUCCESS",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        detail_json={"session_id": str(session_id)} if session_id else None,
        ip_address=ip_address,
    )


async def log_login_failed(ip_address: str, user_id=None) -> None:
    """Record a failed staff login as a `LOGIN_FAILED` audit entry.

    Args:
        user_id: the matched user, if the username resolved but the password
            check failed; `None` means the username itself didn't match a
            user. Either way the entry's `reason` detail reflects which case
            occurred.
    """
    await _audit_logger.record(
        event_type="LOGIN_FAILED",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        detail_json={"reason": "invalid_password" if user_id else "user_not_found"},
        ip_address=ip_address,
    )


async def log_logout(user_id, session_id, ip_address: str) -> None:
    """Record a staff logout as a `LOGOUT` audit entry."""
    await _audit_logger.record(
        event_type="LOGOUT",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        detail_json={"session_id": str(session_id)} if session_id else None,
        ip_address=ip_address,
    )


async def log_access_denied(ip_address: str, user_id=None) -> None:
    """Record a rejected auth attempt as an `ACCESS_DENIED` audit entry.

    Args:
        user_id: the claimed user, if known (e.g. from a JWT that failed
            session-revocation checks); `None` when the request never
            resolved to a user at all (e.g. an invalid/expired token).
    """
    await _audit_logger.record(
        event_type="ACCESS_DENIED",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        ip_address=ip_address,
    )


async def log_patient_login_success(user_id, patient_id, session_id, ip_address: str) -> None:
    """Record a successful patient-portal login as a `PATIENT_LOGIN` audit entry."""
    await _audit_logger.record(
        event_type="PATIENT_LOGIN",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        detail_json={
            "role": "PATIENT",
            "patient_id": str(patient_id),
            **({"session_id": str(session_id)} if session_id else {}),
        },
        ip_address=ip_address,
    )


async def log_patient_login_failed(ip_address: str, patient_id=None) -> None:
    """Record a failed patient-portal login as a `PATIENT_LOGIN_FAILED` audit entry.

    Args:
        patient_id: the matched patient, if identification succeeded but a
            later check (e.g. password) failed; omitted from the detail
            payload when unknown.
    """
    detail: dict = {}
    if patient_id:
        detail["patient_id"] = str(patient_id)
    await _audit_logger.record(
        event_type="PATIENT_LOGIN_FAILED",
        entity_type="auth",
        entity_id=settings.zero_uuid,
        user_id=None,
        detail_json=detail if detail else None,
        ip_address=ip_address,
    )


async def log_patient_logout(user_id, session_id, ip_address: str) -> None:
    """Record a patient-portal logout as a `PATIENT_LOGOUT` audit entry."""
    await _audit_logger.record(
        event_type="PATIENT_LOGOUT",
        entity_type="auth",
        entity_id=user_id or settings.zero_uuid,
        user_id=user_id,
        detail_json={"session_id": str(session_id)} if session_id else None,
        ip_address=ip_address,
    )
