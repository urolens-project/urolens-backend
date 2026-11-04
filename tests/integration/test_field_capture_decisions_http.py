"""HTTP-level tests — Proceed, Discard and Submit for Verification from the phone.

Through the real routes with a real MedTech token:
- the caller's identity reaches the service only from the token;
- refusals leave as the error envelope with their code;
- with the flag off the routes answer 404 `FEATURE_DISABLED`.
The service is replaced with a fake; its behaviour is covered by
tests/test_field_capture_decisions.py, and the role checks by
tests/test_field_route_stubs.py.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from main import app
from src.api.fields import getResultFieldService
from src.core.config import settings
from src.core.exceptions import FieldsBelowMinimumError
from src.models.analysis_result import ResultStatus
from src.schemas.field import (
    FieldActionResponse,
    FieldSummaryItem,
    ResultStatusResponse,
)
from tests.integration.conftest import MEDTECH_USER_ID, TEST_RESULT_ID

FIELD_ID = uuid.UUID("00000000-0000-0000-0000-0000000007b1")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)


def _actionResponse(status: str) -> FieldActionResponse:
    return FieldActionResponse(
        field=FieldSummaryItem(
            fieldId=FIELD_ID, resultId=TEST_RESULT_ID, fieldNo=4, magnification="HPF", status=status,
            aiFindings={}, effectiveCounts={}, confidenceScores={}, version=2, createdAt=NOW, updatedAt=NOW),
        acceptedFieldCount=4, verifiedFieldCount=0, undecidedFieldCount=1)


@pytest.fixture
def fieldService():
    """A fake field service, removed after the test."""
    service = MagicMock()
    service.acceptField = AsyncMock(return_value=_actionResponse("ACCEPTED"))
    service.discardField = AsyncMock(return_value=_actionResponse("DISCARDED"))
    service.submitFields = AsyncMock(return_value=ResultStatusResponse(
        resultId=TEST_RESULT_ID, status=ResultStatus.PENDING_CONFIRM, updatedAt=NOW))
    app.dependency_overrides[getResultFieldService] = lambda: service
    try:
        yield service
    finally:
        app.dependency_overrides.pop(getResultFieldService, None)


@pytest.fixture
def fieldsOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_proceedAcceptsAsTheTokenUser(
    asyncClient: AsyncClient, medtechToken: str, fieldService: MagicMock, fieldsOn: None
) -> None:
    response = await asyncClient.post(f"/api/v1/fields/{FIELD_ID}/accept", headers=_auth(medtechToken))

    assert response.status_code == 200, response.text
    assert response.json()["field"]["status"] == "ACCEPTED"
    assert fieldService.acceptField.await_args.args[:2] == (FIELD_ID, MEDTECH_USER_ID)


@pytest.mark.asyncio
async def test_discardWithoutAReasonSendsNone(
    asyncClient: AsyncClient, medtechToken: str, fieldService: MagicMock, fieldsOn: None
) -> None:
    response = await asyncClient.post(f"/api/v1/fields/{FIELD_ID}/discard", headers=_auth(medtechToken))

    assert response.status_code == 200, response.text
    assert fieldService.discardField.await_args.args[:3] == (FIELD_ID, MEDTECH_USER_ID, None)


@pytest.mark.asyncio
async def test_submitMovesTheResultAsTheTokenUser(
    asyncClient: AsyncClient, medtechToken: str, fieldService: MagicMock, fieldsOn: None
) -> None:
    response = await asyncClient.post(
        f"/api/v1/results/{TEST_RESULT_ID}/fields/submit", headers=_auth(medtechToken))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PENDING_CONFIRM"
    assert fieldService.submitFields.await_args.args[:2] == (TEST_RESULT_ID, MEDTECH_USER_ID)


@pytest.mark.asyncio
async def test_aSubmitBelowTheMinimumUsesTheErrorEnvelope(
    asyncClient: AsyncClient, medtechToken: str, fieldService: MagicMock, fieldsOn: None
) -> None:
    fieldService.submitFields.side_effect = FieldsBelowMinimumError(7, 10)

    response = await asyncClient.post(
        f"/api/v1/results/{TEST_RESULT_ID}/fields/submit", headers=_auth(medtechToken))

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "FIELDS_BELOW_MINIMUM", "message": "7 of 10 required fields accepted."}}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"/api/v1/fields/{FIELD_ID}/accept",
    f"/api/v1/results/{TEST_RESULT_ID}/fields/submit",
])
async def test_withTheFlagOffTheRoutesAreNotThere(
    asyncClient: AsyncClient, medtechToken: str, fieldService: MagicMock,
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)

    response = await asyncClient.post(path, headers=_auth(medtechToken))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FEATURE_DISABLED"
    fieldService.acceptField.assert_not_awaited()
    fieldService.submitFields.assert_not_awaited()
