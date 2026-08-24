"""Shared fixtures for image-upload / AI-inference integration tests.

Architecture
------------
- No real database: the Supabase client is replaced with an AsyncMock whose
  `.table().select/update/insert/execute()` chain can be configured per-test.
- JWT tokens are real HS256 tokens signed with the app's JWT_SIGNING_KEY so
  the RequireRole middleware accepts them without change.
- The FastAPI app is driven via httpx.AsyncClient + ASGITransport (no real
  network sockets required).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Import app after env is loaded (main.py calls load_dotenv at top)
from main import app
from src.core.config import settings

# ── Fixed IDs ────────────────────────────────────────────────────────────────

MEDTECH_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
TEST_IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


# ── JWT fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def medtechToken() -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(MEDTECH_USER_ID),
        "role": "MEDTECH",
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest.fixture
def testSpecimen() -> uuid.UUID:
    return TEST_SPECIMEN_ID


@pytest_asyncio.fixture(autouse=True)
async def mockSessionActive():
    """The canonical auth dependency (src.core.rbac, adopted by image.py
    in the image/AI domain merge) checks session revocation via
    is_session_active(), which hits Supabase — unlike the old non-canonical
    dependency these tests were originally written against, which only
    decoded the JWT locally. Mocked here (autouse) so the plain signed-JWT
    fixtures above don't need a real `sessions` table.
    """
    with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
        yield


# ── Supabase mock builder ────────────────────────────────────────────────────

def _makeSbMock(
    imagesRows: list[dict] | None = None,
    analysisRows: list[dict] | None = None,
) -> MagicMock:
    """Build a mock Supabase client whose chained call pattern mirrors the real
    async client:  sb.table(name).select(...).eq(...).execute()  → APIResponse

    Parameters
    ----------
    images_rows:    rows returned by SELECT on the `images` table
    analysis_rows:  rows returned by SELECT on the `analysis_results` table
    """
    imagesRows = imagesRows or []
    analysisRows = analysisRows or []

    def _makeResp(rows: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.data = rows
        return resp

    async def _executeImages():
        return _makeResp(imagesRows)

    async def _executeAnalysis():
        return _makeResp(analysisRows)

    async def _executeEmpty():
        return _makeResp([])

    # Build a flexible query-builder mock
    def _queryBuilder(tableName: str):
        qb = MagicMock()
        # All chainable methods return self so we can do .select().eq().limit()
        qb.select.return_value = qb
        qb.eq.return_value = qb
        qb.limit.return_value = qb
        qb.update.return_value = qb
        qb.insert.return_value = qb

        if tableName == "images":
            qb.execute = AsyncMock(side_effect=_executeImages)
        elif tableName == "analysis_results":
            qb.execute = AsyncMock(side_effect=_executeAnalysis)
        else:
            qb.execute = AsyncMock(side_effect=_executeEmpty)

        return qb

    sbMock = MagicMock()
    sbMock.table.side_effect = _queryBuilder

    # Storage mock (always succeeds silently)
    storageMock = MagicMock()
    bucketMock = MagicMock()
    bucketMock.upload = AsyncMock(return_value=MagicMock())
    storageMock.from_.return_value = bucketMock
    sbMock.storage = storageMock

    return sbMock


# ── HTTP client fixture ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── Composite fixture: client + fresh Supabase mock ──────────────────────────

@pytest_asyncio.fixture
async def clientWithMockSb(asyncClient):
    """Yields (async_client, sb_mock).

    Patches both the image-router and the retake-service Supabase references so
    both use the same mock instance. Starts with no existing DB rows (empty
    images and analysis_results tables).
    """
    sbMock = _makeSbMock(
        imagesRows=[],
        analysisRows=[],
    )
    with (
        patch("src.api.image.sb", sbMock),
        patch("src.services.image_retake_service.sb", sbMock),
    ):
        yield asyncClient, sbMock
