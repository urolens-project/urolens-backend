"""HTTP-level tests — overriding an AI-detected value (UROLENS-227).

Through the real `POST /results/{id}/override` route and the real
`ManualOverrideService` (DB mocked): request validation (whole counts 0–300, a
required rationale) answers with the 422 envelope, an unchanged value gets
`OVERRIDE_UNCHANGED`, and a real correction is saved. `GET /sync/pull` carries
the overrides to the phone.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import Select

from main import app
from src.api.results import getOverrideService
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.specimen import Specimen
from src.services.manual_override_service import ManualOverrideService
from tests.conftest import makeSyncDb
from tests.integration.conftest import MEDTECH_USER_ID

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000002b1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000002b2")


def _headers() -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "user_id": str(MEDTECH_USER_ID),
            "role": "MEDTECH",
            "session_id": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwtSigningKey,
        algorithm=settings.jwtAlgorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def _db(latestOverride: str | None = None) -> AsyncMock:
    """The MedTech's own PENDING_CONFIRM result, AI count 7 for `erythrocytes`."""
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_CONFIRM
    result.aiFindings = {"erythrocytes": 7}
    result.particleClasses = {}
    specimen = MagicMock(spec=Specimen)
    specimen.medtechId = MEDTECH_USER_ID

    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = latestOverride if entity is ManualOverride else result
        return executeResult

    async def _refresh(obj: ManualOverride) -> None:
        # What the DB fills in on insert.
        obj.overrideId = uuid.uuid4()
        obj.overriddenAt = datetime.now(UTC)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(return_value=specimen)
    db.add = MagicMock()
    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.fixture
def useDb() -> Iterator[AsyncMock]:
    db = _db()
    app.dependency_overrides[getOverrideService] = lambda: ManualOverrideService(
        db=db, auditLogger=MagicMock(record=AsyncMock())
    )
    yield db
    app.dependency_overrides.pop(getOverrideService, None)


async def _post(asyncClient: AsyncClient, body: dict) -> object:
    return await asyncClient.post(f"/api/v1/results/{RESULT_ID}/override", json=body, headers=_headers())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"parameter": "erythrocytes", "correctedValue": 301, "rationale": "Recounted"},
        {"parameter": "erythrocytes", "correctedValue": 3.7, "rationale": "Recounted"},
        {"parameter": "erythrocytes", "correctedValue": -1, "rationale": "Recounted"},
        {"parameter": "erythrocytes", "correctedValue": 4},
        {"parameter": "erythrocytes", "correctedValue": 4, "rationale": "   "},
    ],
    ids=["over-300", "fraction", "negative", "no-rationale", "blank-rationale"],
)
async def test_invalidOverridesAreRejectedBeforeReachingTheService(
    asyncClient: AsyncClient, useDb: AsyncMock, body: dict
) -> None:
    response = await _post(asyncClient, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    useDb.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_anUnchangedValueGetsOverrideUnchanged(asyncClient: AsyncClient, useDb: AsyncMock) -> None:
    response = await _post(asyncClient, {"parameter": "erythrocytes", "correctedValue": 7, "rationale": "Recounted"})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "OVERRIDE_UNCHANGED"
    useDb.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aRealCorrectionIsSavedWithTheAiOriginal(asyncClient: AsyncClient, useDb: AsyncMock) -> None:
    response = await _post(
        asyncClient,
        {"parameter": "erythrocytes", "correctedValue": 300, "rationale": "  Recounted  ", "originalAiValue": 999},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["correctedValue"], body["originalAiValue"], body["rationale"]) == (300, 7, "Recounted")
    assert body["overriddenBy"] == str(MEDTECH_USER_ID)
    useDb.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_theWebSupervisorPayloadIsAccepted(asyncClient: AsyncClient, useDb: AsyncMock) -> None:
    # What the web's saveOverride sends after its snake_case -> camelCase bridge.
    response = await _post(
        asyncClient,
        {"parameterName": "erythrocytes", "correctedValue": 4, "rationale": "Recounted", "originalAiValue": 7},
    )

    assert response.status_code == 200, response.text
    assert response.json()["parameter"] == "erythrocytes"
    useDb.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_syncSendsTheOverridesOnTheMedtechsResultsToThePhone(asyncClient: AsyncClient) -> None:
    override = ManualOverride(
        overrideId=uuid.uuid4(), resultId=RESULT_ID, parameterName="erythrocytes", originalAiValue="7.0",
        correctedValue="4.0", rationale="Recounted", medtechId=uuid.uuid4(), overriddenAt=datetime.now(UTC),
    )
    db = makeSyncDb(overrides=[override])

    async def _getDb() -> AsyncIterator[AsyncMock]:
        yield db

    app.dependency_overrides[getDb] = _getDb
    try:
        response = await asyncClient.get("/api/v1/sync/pull", headers=_headers())
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    [row] = response.json()["changes"]["manualOverrides"]["created"]
    assert (row["parameter_name"], row["original_ai_value"], row["corrected_value"]) == ("erythrocytes", 7.0, 4.0)
    assert row["result_id"] == str(RESULT_ID)
