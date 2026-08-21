"""
Shared fixtures for image-upload / AI-inference integration tests.

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
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.urolens.core.config import JWT_ALGORITHM, JWT_SIGNING_KEY

# Import app after env is loaded (main.py calls load_dotenv at top)
from main import app


# ── Fixed IDs ────────────────────────────────────────────────────────────────

MEDTECH_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
TEST_IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
TEST_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


# ── JWT fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def medtech_token() -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": str(MEDTECH_USER_ID),
        "role": "MEDTECH",
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, JWT_SIGNING_KEY, algorithm=JWT_ALGORITHM)


@pytest.fixture
def test_specimen() -> uuid.UUID:
    return TEST_SPECIMEN_ID


@pytest_asyncio.fixture(autouse=True)
async def mock_session_active():
    """
    The canonical auth dependency (app.middleware.rbac, adopted by image.py
    in the image/AI domain merge) checks session revocation via
    is_session_active(), which hits Supabase — unlike the old non-canonical
    dependency these tests were originally written against, which only
    decoded the JWT locally. Mocked here (autouse) so the plain signed-JWT
    fixtures above don't need a real `sessions` table.
    """
    with patch("app.middleware.rbac.is_session_active", AsyncMock(return_value=True)):
        yield


# ── Supabase mock builder ────────────────────────────────────────────────────

def _make_sb_mock(
    images_rows: list[dict] | None = None,
    analysis_rows: list[dict] | None = None,
) -> MagicMock:
    """
    Build a mock Supabase client whose chained call pattern mirrors the real
    async client:  sb.table(name).select(...).eq(...).execute()  → APIResponse

    Parameters
    ----------
    images_rows:    rows returned by SELECT on the `images` table
    analysis_rows:  rows returned by SELECT on the `analysis_results` table
    """
    images_rows = images_rows or []
    analysis_rows = analysis_rows or []

    def _make_resp(rows: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.data = rows
        return resp

    async def _execute_images():
        return _make_resp(images_rows)

    async def _execute_analysis():
        return _make_resp(analysis_rows)

    async def _execute_empty():
        return _make_resp([])

    # Build a flexible query-builder mock
    def _query_builder(table_name: str):
        qb = MagicMock()
        # All chainable methods return self so we can do .select().eq().limit()
        qb.select.return_value = qb
        qb.eq.return_value = qb
        qb.limit.return_value = qb
        qb.update.return_value = qb
        qb.insert.return_value = qb

        if table_name == "images":
            qb.execute = AsyncMock(side_effect=_execute_images)
        elif table_name == "analysis_results":
            qb.execute = AsyncMock(side_effect=_execute_analysis)
        else:
            qb.execute = AsyncMock(side_effect=_execute_empty)

        return qb

    sb_mock = MagicMock()
    sb_mock.table.side_effect = _query_builder

    # Storage mock (always succeeds silently)
    storage_mock = MagicMock()
    bucket_mock = MagicMock()
    bucket_mock.upload = AsyncMock(return_value=MagicMock())
    storage_mock.from_.return_value = bucket_mock
    sb_mock.storage = storage_mock

    return sb_mock


# ── HTTP client fixture ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── Composite fixture: client + fresh Supabase mock ──────────────────────────

@pytest_asyncio.fixture
async def client_with_mock_sb(async_client):
    """
    Yields (async_client, sb_mock).

    Patches both the image-router and the retake-service Supabase references so
    both use the same mock instance. Starts with no existing DB rows (empty
    images and analysis_results tables).
    """
    sb_mock = _make_sb_mock(
        images_rows=[],
        analysis_rows=[],
    )
    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        yield async_client, sb_mock
