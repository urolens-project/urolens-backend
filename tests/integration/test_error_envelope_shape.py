"""Integration tests — HTTP-level proof that the C1 double-nested error
envelope is fixed on the two mounted endpoints the readiness audit flagged
as still live: `POST /api/v1/queue/assign` (queue_service.assignSpecimen)
and `POST /api/v1/results/{id}/release` (result_releasing_service.releaseResult).

Both previously built their own `detail={"error": {"code": ..., "message":
..., "details": {}}}` dict, which main.py's global HTTPException handler
then wrapped in a second `{"error": {...}}` layer, producing
`{"error": {"code": "ERROR", "message": {"error": {"code": "...", ...}}}}`
on the wire. They now raise the typed exceptions from `src/core/exceptions.py`
(which only set `.errorCode`), so the handler produces a single-level
envelope. Asserting `response.json()["error"]["code"]` directly (a plain
string, not a nested dict) is itself part of the proof — the old shape would
fail that assertion with a `TypeError` on `.json()["error"]["message"]`
indexing, not just a wrong value.
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
from src.core.supabase import getSupabase
from src.models.analysis_result import AnalysisResult

RECEPTIONIST_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000080")
TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000081")


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


# ── POST /api/v1/queue/assign ──────────────────────────────────────────────────

def _emptySupabaseChain() -> MagicMock:
    """A Supabase query-builder mock whose `.execute()` always reports no rows —
    drives `assignSpecimen` down its "specimen not found" branch.
    """
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.execute = AsyncMock(return_value=MagicMock(data=[]))
    return chain


@pytest.fixture
def mockQueueDeps():
    """Overrides both `getSupabase` (backs the specimen/medtech/assignment
    lookups) and `getDb` (backs the audit logger / notification service) so
    the request never touches a real database.
    """
    sb = MagicMock()
    sb.table.side_effect = lambda _name: _emptySupabaseChain()

    sqlDb = AsyncMock()

    async def _overrideSupabase():
        return sb

    async def _overrideDb():
        yield sqlDb

    app.dependency_overrides[getSupabase] = _overrideSupabase
    app.dependency_overrides[getDb] = _overrideDb
    try:
        yield sb
    finally:
        app.dependency_overrides.pop(getSupabase, None)
        app.dependency_overrides.pop(getDb, None)


@pytest.mark.asyncio
async def test_queueAssignSpecimenNotFoundReturnsFlatEnvelope(asyncClient, mockQueueDeps):
    token = _mintReceptionistToken()
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        response = await asyncClient.post(
            "/api/v1/queue/assign",
            headers={"Authorization": f"Bearer {token}"},
            json={"specimenId": str(uuid.uuid4()), "medtechId": str(uuid.uuid4())},
        )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "SPECIMEN_NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


# ── POST /api/v1/results/{result_id}/release ──────────────────────────────────

@pytest.fixture
def mockResultReleasingDbNotApproved():
    """Overrides `getDb` so the looked-up `AnalysisResult` exists but isn't
    `APPROVED` — drives `releaseResult` down its `RESULT_NOT_APPROVED` branch.
    """
    ar = MagicMock(spec=AnalysisResult)
    ar.resultId = TEST_RESULT_ID
    ar.status = "PENDING_CONFIRM"

    db = AsyncMock()

    async def _get(model, id_):
        return ar if id_ == TEST_RESULT_ID else None

    db.get = AsyncMock(side_effect=_get)

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


@pytest.mark.asyncio
async def test_releaseResultNotApprovedReturnsFlatEnvelope(
    asyncClient, mockResultReleasingDbNotApproved
):
    token = _mintReceptionistToken()
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        response = await asyncClient.post(
            f"/api/v1/results/{TEST_RESULT_ID}/release",
            headers={"Authorization": f"Bearer {token}"},
            json={"releaseMethod": "PHYSICAL"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "RESULT_NOT_APPROVED"
    assert isinstance(body["error"]["message"], str)
