"""Integration test — HTTP-level proof for UROLENS-141 item 5's minimum
query-length enforcement on `GET /api/v1/specimens/search-received`.

`q` is validated by FastAPI's own `Query(min_length=3)` at the route (see
`src/api/labeling.py`), not re-checked inside `labeling_service`, so this
needs a real mounted route to prove the standard flat `VALIDATION_ERROR`
envelope actually reaches the HTTP response — a unit test calling the
service function directly can't exercise route-level parameter validation
at all.
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

MEDTECH_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000110")


def _mintMedtechToken() -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(MEDTECH_USER_ID),
        "role": "MEDTECH",
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
def mockDb():
    db = AsyncMock()
    executeResult = AsyncMock()
    db.execute = AsyncMock(return_value=executeResult)

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["", "a", "ab"])
async def test_searchReceivedRejectsQueryUnderMinLength(asyncClient, mockDb, q):
    token = _mintMedtechToken()
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        response = await asyncClient.get(
            "/api/v1/specimens/search-received",
            params={"q": q},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "q" in body["error"]["details"]
    mockDb.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_searchReceivedRejectsMissingQuery(asyncClient, mockDb):
    token = _mintMedtechToken()
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        response = await asyncClient.get(
            "/api/v1/specimens/search-received",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
