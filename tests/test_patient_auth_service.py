"""Unit tests — patient_auth_service.patientLogin.

Covers the fix replacing the guessable "lastname + DOB, re-derived on every
attempt" password scheme with a real check against the bcrypt hash stored on
the patient's linked `users` row (the same `verifyPassword` staff login
uses). Previously untested — this is new coverage for a Tier 1 (auth) path,
not a port of pre-existing tests.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.core.auth_service import hashPassword
from src.services import patient_auth_service

PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000070")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000071")
SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")


def _patientRow(**overrides) -> dict:
    row = {"patient_id": str(PATIENT_ID), "user_id": str(USER_ID)}
    row.update(overrides)
    return row


def _userRow(**overrides) -> dict:
    row = {
        "user_id": str(USER_ID),
        "hashed_password": hashPassword("REALPASSWORD1"),
        "locked_at": None,
        "is_active": True,
    }
    row.update(overrides)
    return row


def _mockSupabase(patientRow: dict | None, userRow: dict | None) -> MagicMock:
    def _queryBuilder(tableName: str):
        qb = MagicMock()
        qb.select.return_value = qb
        qb.eq.return_value = qb
        row = patientRow if tableName == "patients" else userRow
        qb.maybe_single.return_value = qb
        qb.execute = AsyncMock(return_value=MagicMock(data=row))
        return qb

    sb = MagicMock()
    sb.table.side_effect = _queryBuilder
    return sb


@pytest.fixture(autouse=True)
def _patchSideEffects():
    with (
        patch.object(patient_auth_service.audit_logger, "logPatientLoginFailed", AsyncMock()),
        patch.object(patient_auth_service.audit_logger, "logPatientLoginSuccess", AsyncMock()),
        patch.object(patient_auth_service, "incrementFailedAttempts", AsyncMock()),
        patch.object(patient_auth_service, "resetFailedAttempts", AsyncMock()),
        patch.object(
            patient_auth_service,
            "createSession",
            AsyncMock(return_value={"session_id": str(SESSION_ID)}),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_patientLoginSucceedsWithCorrectPassword():
    sb = _mockSupabase(_patientRow(), _userRow())
    with patch.object(patient_auth_service, "supabase", sb):
        response = await patient_auth_service.patientLogin(
            "PAT-000001", "REALPASSWORD1", request=MagicMock(client=None, headers={})
        )
    assert response.role == "PATIENT"
    assert response.userId == str(USER_ID)


@pytest.mark.asyncio
async def test_patientLoginRejectsWrongPassword():
    sb = _mockSupabase(_patientRow(), _userRow())
    with patch.object(patient_auth_service, "supabase", sb):
        with pytest.raises(HTTPException) as excInfo:
            await patient_auth_service.patientLogin(
                "PAT-000001", "totally-wrong", request=MagicMock(client=None, headers={})
            )
    assert excInfo.value.status_code == 401
    assert excInfo.value.errorCode == "INVALID_CREDENTIALS"
    patient_auth_service.incrementFailedAttempts.assert_awaited_once()


@pytest.mark.asyncio
async def test_patientLoginRejectsNameDobDerivedPasswordNoLongerAccepted():
    """Regression guard: the password scheme this replaces derived
    LASTNAME + DDMMYYYY from the patient's own (guessable) name/DOB. That
    value must no longer authenticate now that login checks a real hash.
    """
    sb = _mockSupabase(_patientRow(), _userRow())
    with patch.object(patient_auth_service, "supabase", sb):
        with pytest.raises(HTTPException) as excInfo:
            await patient_auth_service.patientLogin(
                "PAT-000001", "DOE01012000", request=MagicMock(client=None, headers={})
            )
    assert excInfo.value.status_code == 401


@pytest.mark.asyncio
async def test_patientLoginUnknownPatientUidRejected():
    sb = _mockSupabase(None, None)
    with patch.object(patient_auth_service, "supabase", sb):
        with pytest.raises(HTTPException) as excInfo:
            await patient_auth_service.patientLogin(
                "PAT-999999", "whatever", request=MagicMock(client=None, headers={})
            )
    assert excInfo.value.errorCode == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_patientLoginLockedAccountRejectedAfterPasswordCheck():
    sb = _mockSupabase(_patientRow(), _userRow(locked_at="2026-09-24T00:00:00+00:00"))
    with patch.object(patient_auth_service, "supabase", sb):
        with pytest.raises(HTTPException) as excInfo:
            await patient_auth_service.patientLogin(
                "PAT-000001", "REALPASSWORD1", request=MagicMock(client=None, headers={})
            )
    assert excInfo.value.status_code == 423
    assert excInfo.value.errorCode == "ACCOUNT_LOCKED"


@pytest.mark.asyncio
async def test_patientLoginInactiveAccountRejected():
    sb = _mockSupabase(_patientRow(), _userRow(is_active=False))
    with patch.object(patient_auth_service, "supabase", sb):
        with pytest.raises(HTTPException) as excInfo:
            await patient_auth_service.patientLogin(
                "PAT-000001", "REALPASSWORD1", request=MagicMock(client=None, headers={})
            )
    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "ACCOUNT_INACTIVE"
