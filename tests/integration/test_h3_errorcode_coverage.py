"""Integration tests — HTTP-level proof for the remaining H3 fix sites.

`tests/integration/test_error_envelope_shape.py` already proves the two C1
double-nested-envelope endpoints (queue/assign, results/release) now return a
flat `{"error": {"code", "message"}}` body. This file does the same for one
representative site in each of the other seven files Step 1 touched
(previously bare `HTTPException`s with no `.errorCode`, silently falling
back to a generic code) — one HTTP-level test per service, hitting the real
mounted route through `httpx.AsyncClient`, not just asserting on the raised
exception object.

Services covered here, one site each:
- physician_result_service.py  -> RESULT_NOT_FOUND  (404)
- labeling_service.py           -> LABEL_NOT_FOUND    (400)
- lab_request_service.py        -> REQUEST_UID_GENERATION_FAILED (500)
- patient_service.py            -> CONFLICT            (409)
- notifications.py              -> NOT_FOUND           (404)
- result_review_service.py      -> RESULT_NOT_FOUND    (404)
- specimen_service.py           -> INVALID_REJECTION_REASON (422)
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.core.encryption import encryptPii
from src.models.patient import Patient

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000090")
TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000091")
TEST_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000092")
TEST_LABEL_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000093")
TEST_NOTIFICATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000094")
TEST_PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000095")


def _mintToken(role: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(USER_ID),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _overrideDb(db):
    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override


def _clearDbOverride():
    app.dependency_overrides.pop(getDb, None)


# ── physician_result_service.py: GET /api/v1/physician/results/{id} ───────────

@pytest.mark.asyncio
async def test_physicianResultDetailNotFoundReturnsFlatEnvelope(asyncClient):
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    _overrideDb(db)
    try:
        token = _mintToken("PHYSICIAN")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                f"/api/v1/physician/results/{TEST_RESULT_ID}",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "RESULT_NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


# ── labeling_service.py: POST /api/v1/specimens/{id}/label/confirm ────────────

@pytest.mark.asyncio
async def test_confirmLabelAffixedNoLabelReturnsFlatEnvelope(asyncClient):
    db = AsyncMock()
    executeResult = MagicMock()
    executeResult.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=executeResult)
    _overrideDb(db)
    try:
        token = _mintToken("MEDTECH")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/specimens/{TEST_LABEL_SPECIMEN_ID}/label/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={"offlineOverride": False},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "LABEL_NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


# ── lab_request_service.py: POST /api/v1/lab-requests ──────────────────────────

@pytest.mark.asyncio
async def test_createLabRequestUidExhaustionReturnsFlatEnvelope(asyncClient):
    """Forces every UID-collision check to report a match, exhausting
    `_UID_GENERATION_ATTEMPTS` retries and hitting the 500
    `REQUEST_UID_GENERATION_FAILED` path.
    """
    db = AsyncMock()
    db.get = AsyncMock(return_value=MagicMock(spec=Patient))

    alwaysCollides = MagicMock()
    alwaysCollides.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=alwaysCollides)

    _overrideDb(db)
    try:
        token = _mintToken("RECEPTIONIST")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                "/api/v1/lab-requests",
                headers={"Authorization": f"Bearer {token}"},
                json={"patientId": str(TEST_PATIENT_ID), "testType": "Urinalysis"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "REQUEST_UID_GENERATION_FAILED"
    assert isinstance(body["error"]["message"], str)


# ── patient_service.py: POST /api/v1/patients ──────────────────────────────────

@pytest.mark.asyncio
async def test_createPatientDuplicateReturnsFlatEnvelope(asyncClient):
    db = AsyncMock()
    existingRow = (encryptPii("Jane"), encryptPii("Doe"), encryptPii("2000-01-01"))
    duplicateCheck = MagicMock()
    duplicateCheck.all.return_value = [existingRow]
    db.execute = AsyncMock(return_value=duplicateCheck)

    _overrideDb(db)
    try:
        token = _mintToken("RECEPTIONIST")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                "/api/v1/patients",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "dateOfBirth": "2000-01-01",
                    "sex": "FEMALE",
                    "isWalkin": False,
                    "consent": {
                        "consentGiven": True,
                        "consentStorage": True,
                        "consentResearch": False,
                    },
                },
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CONFLICT"
    assert isinstance(body["error"]["message"], str)


# ── notifications.py: PATCH /api/v1/notifications/{id}/read ───────────────────

@pytest.mark.asyncio
async def test_markNotificationReadNotFoundReturnsFlatEnvelope(asyncClient):
    db = AsyncMock()
    updateResult = MagicMock()
    updateResult.rowcount = 0
    db.execute = AsyncMock(return_value=updateResult)
    _overrideDb(db)
    try:
        token = _mintToken("MEDTECH")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.patch(
                f"/api/v1/notifications/{TEST_NOTIFICATION_ID}/read",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


# ── result_review_service.py: GET /api/v1/results/{id}/smart-diagnosis ────────

@pytest.mark.asyncio
async def test_getSmartDiagnosisNotFoundReturnsFlatEnvelope(asyncClient):
    emptyChain = MagicMock()
    emptyChain.select.return_value = emptyChain
    emptyChain.eq.return_value = emptyChain
    emptyChain.execute = AsyncMock(return_value=MagicMock(data=[]))

    sbMock = MagicMock()
    sbMock.table.return_value = emptyChain

    token = _mintToken("SUPERVISOR")
    with (
        patch("src.services.result_review_service.supabase", sbMock),
        patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)),
    ):
        # No DB dependency override needed: getSmartDiagnosis is pure Supabase REST.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/results/{TEST_RESULT_ID}/smart-diagnosis",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "RESULT_NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


# ── specimen_service.py: POST /api/v1/specimens/{id}/reject ───────────────────

@pytest.mark.asyncio
async def test_rejectSpecimenInvalidReasonReturnsFlatEnvelope(asyncClient):
    """`reasonCode` is validated before any DB access, so a bare, unused mock
    `AsyncSession` is enough to exercise this path end-to-end over HTTP.
    """
    db = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken("MEDTECH")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/specimens/{TEST_SPECIMEN_ID}/reject",
                headers={"Authorization": f"Bearer {token}"},
                json={"reasonCode": "NOT_A_REAL_REASON"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_REJECTION_REASON"
    assert isinstance(body["error"]["message"], str)
