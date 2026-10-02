"""HTTP-level tests — spatial annotation bounding boxes (UROLENS-149).

Through the real `PATCH /results/{id}/annotate` route (DB mocked): a
`spatialAnnotations` item with an unknown `particleType`, or missing its
`w`/`h` box dimensions, is rejected with the 422 envelope before the service
is ever reached; a full box with a known `particleType` round-trips
correctly.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from httpx import AsyncClient

from main import app
from src.api.results import getResultReviewService
from src.core.config import settings
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.services.result_review_service import ResultReviewService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000003b1")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000003b2")


def _headers() -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "user_id": str(SUPERVISOR_ID),
            "role": "SUPERVISOR",
            "session_id": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwtSigningKey,
        algorithm=settings.jwtAlgorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def _db() -> AsyncMock:
    """A result under active supervisor review, with no existing annotation row."""
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL

    resultLookup = MagicMock()
    resultLookup.scalar_one_or_none.return_value = result
    noExistingReview = MagicMock()
    noExistingReview.scalar_one_or_none.return_value = None

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[resultLookup, noExistingReview])
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def useDb() -> Iterator[AsyncMock]:
    db = _db()
    app.dependency_overrides[getResultReviewService] = lambda: ResultReviewService(
        db=db, auditLogger=MagicMock(record=AsyncMock())
    )
    yield db
    app.dependency_overrides.pop(getResultReviewService, None)


async def _patch(asyncClient: AsyncClient, body: dict) -> object:
    return await asyncClient.patch(
        f"/api/v1/results/{RESULT_ID}/annotate", json=body, headers=_headers()
    )


@pytest.mark.asyncio
async def test_unknownParticleTypeIsRejectedBeforeReachingTheService(
    asyncClient: AsyncClient, useDb: AsyncMock
) -> None:
    body = {
        "annotationNotes": "Possible cast cluster",
        "spatialAnnotations": [
            {"id": "a1", "x": 10, "y": 20, "w": 5, "h": 5, "particleType": "not_a_real_particle"}
        ],
    }
    response = await _patch(asyncClient, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    useDb.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missingBoxDimensionsRejectedBeforeReachingTheService(
    asyncClient: AsyncClient, useDb: AsyncMock
) -> None:
    """UROLENS-149 bug fix: the frontend's AnnotationCanvas sends a bounding
    box (id/x/y/w/h/particleType) — w/h are required, not optional, since
    every annotation this UI has ever produced is a box.
    """
    body = {
        "annotationNotes": "Possible cast cluster",
        "spatialAnnotations": [{"id": "a1", "x": 10, "y": 20, "particleType": "urinary_casts"}],
    }
    response = await _patch(asyncClient, body)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    useDb.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_knownParticleTypeRoundTripsCorrectly(asyncClient: AsyncClient, useDb: AsyncMock) -> None:
    body = {
        "annotationNotes": "Possible cast cluster",
        "spatialAnnotations": [
            {"id": "a1", "x": 10, "y": 20, "w": 5, "h": 8, "particleType": "urinary_casts"}
        ],
    }
    response = await _patch(asyncClient, body)

    assert response.status_code == 200, response.text
    [item] = response.json()["spatialAnnotations"]
    assert (item["id"], item["x"], item["y"], item["w"], item["h"], item["particleType"]) == (
        "a1", 10, 20, 5, 8, "urinary_casts"
    )
    useDb.commit.assert_awaited_once()
