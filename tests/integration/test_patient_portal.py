"""Integration tests — patient-portal result routes share
`PatientResultService.getResultDetail`'s RELEASED status gate, including the
PDF-download route (which calls `getResultDetail` internally).

`pdf_service.generateResultPdf` reads fields (`cellCounts`, `createdAt`,
`pathologistName`, ...) that aren't on `PatientResultDetailResponse` — a
pre-existing mismatch unrelated to this migration (confirmed: it predates it
and isn't one of the bugs the migration plan flagged for these 4 services).
That means a *successful* PDF render can't be exercised here without masking
this migration's own test coverage behind an unrelated bug; these tests
instead confirm the non-released case is rejected before
`generateResultPdf` is ever reached — the actual gate under test.

Assertions below check the error message, not error.code — a second,
unrelated pre-existing mismatch: main.py's HTTPException handler reads
`exc.error_code` (snake_case) but every service's custom exceptions set
`.errorCode` (camelCase, matching this codebase's attribute-naming
convention) — so a specific code like RESULT_NOT_RELEASED never survives
the trip over HTTP; the client only ever sees the generic status-based
fallback (FORBIDDEN, NOT_FOUND, ...). Confirmed present before this
migration touched anything; out of scope to fix here.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from main import app
from src.api.patient_portal import getPatientResultService
from src.core.config import settings

PATIENT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000060")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000061")


def _mintToken(userId: uuid.UUID, role: str = "PATIENT") -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


def _notReleasedService() -> MagicMock:
    fakeService = MagicMock()

    async def _raiseNotReleased(*args, **kwargs):
        exc = HTTPException(status_code=403, detail="Result is not yet released.")
        exc.errorCode = "RESULT_NOT_RELEASED"
        raise exc

    fakeService.getResultDetail = AsyncMock(side_effect=_raiseNotReleased)
    return fakeService


@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestStatusGateAppliesToDetailRoute:
    @pytest.mark.asyncio
    async def test_detailRouteRejectsNonReleasedResult(self, asyncClient):
        fakeService = _notReleasedService()
        app.dependency_overrides[getPatientResultService] = lambda: fakeService
        token = _mintToken(PATIENT_USER_ID)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
                response = await asyncClient.get(
                    f"/api/v1/patient/results/{RESULT_ID}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        finally:
            app.dependency_overrides.pop(getPatientResultService, None)

        assert response.status_code == 403
        assert "not yet released" in response.json()["error"]["message"].lower()


class TestStatusGateAppliesToPdfRoute:
    @pytest.mark.asyncio
    async def test_pdfDownloadRejectsNonReleasedResult(self, asyncClient):
        fakeService = _notReleasedService()
        app.dependency_overrides[getPatientResultService] = lambda: fakeService
        token = _mintToken(PATIENT_USER_ID)
        try:
            with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
                response = await asyncClient.get(
                    f"/api/v1/patient/results/{RESULT_ID}/pdf",
                    headers={"Authorization": f"Bearer {token}"},
                )
        finally:
            app.dependency_overrides.pop(getPatientResultService, None)

        assert response.status_code == 403
        assert "not yet released" in response.json()["error"]["message"].lower()


class TestRBAC:
    @pytest.mark.asyncio
    async def test_medtechCannotAccessPatientResults(self, asyncClient):
        token = _mintToken(PATIENT_USER_ID, role="MEDTECH")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/patient/results",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 403
