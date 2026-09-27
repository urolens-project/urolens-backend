"""Integration test — HTTP-level proof for main.py's errorCode fix
(`exc.error_code` snake_case → `.errorCode` camelCase), third distinct
service: `lab_request_service.createLabRequest` raises `NotFoundException`
(`src/core/exceptions.py`, the `.errorCode`-attribute pattern) when the
given `patientId` doesn't exist. Confirms `PATIENT_NOT_FOUND` reaches the
real JSON response body over the receptionist-facing creation route, not
just the raised exception object.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb

RECEPTIONIST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000070")


def _mintReceptionistToken() -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(RECEPTIONIST_USER_ID),
        "role": "RECEPTIONIST",
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def mockDbNoPatient():
    """Overrides Depends(getDb) so `db.get(Patient, ...)` returns None —
    the "patient doesn't exist" path `createLabRequest` checks first, before
    anything else in the function runs.
    """
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


@pytest.mark.asyncio
async def test_createLabRequestReturnsPatientNotFoundCodeInResponseBody(
    asyncClient, mockDbNoPatient
):
    token = _mintReceptionistToken()
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        response = await asyncClient.post(
            "/api/v1/lab-requests",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "patientId": str(uuid.uuid4()),
                "testType": "Urinalysis",
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PATIENT_NOT_FOUND"
    assert response.json()["error"]["message"] == "Patient not found."
