"""HTTP-level tests — `GET /results/medtech/history` and sync removals (UROLENS-236).

Through the real routes and services (DB mocked): the history endpoint is
MedTech-only, validates its category and paging, lists the caller's own
samples (identity from the token) by patient code only; sync returns each
table's `deleted` list on a delta.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from httpx import AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.core.encryption import encryptPii
from tests.conftest import makeSyncDb, syncSpecimen
from tests.integration.conftest import MEDTECH_USER_ID


def _headers(role: str = "MEDTECH") -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "user_id": str(MEDTECH_USER_ID if role == "MEDTECH" else uuid.uuid4()),
            "role": role,
            "session_id": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwtSigningKey,
        algorithm=settings.jwtAlgorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def _useDb(db: AsyncMock) -> None:
    async def _override() -> AsyncIterator[AsyncMock]:
        yield db

    app.dependency_overrides[getDb] = _override


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["SUPERVISOR", "RECEPTIONIST", "PHYSICIAN", "PATIENT"])
async def test_historyIsForbiddenToEveryRoleButMedtech(asyncClient: AsyncClient, role: str) -> None:
    response = await asyncClient.get(
        "/api/v1/results/medtech/history", params={"category": "APPROVED"}, headers=_headers(role)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{}, {"category": "RETURNED_FOR_CORRECTION"}, {"category": "APPROVED", "pageSize": 101}, {"category": "APPROVED", "page": 0}],
    ids=["no-category", "unknown-category", "page-too-big", "page-zero"],
)
async def test_historyValidatesItsQuery(asyncClient: AsyncClient, params: dict) -> None:
    response = await asyncClient.get("/api/v1/results/medtech/history", params=params, headers=_headers())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_historyListsTheCallersSamplesByPatientCodeOnly(asyncClient: AsyncClient) -> None:
    rejectedAt = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    specimen = syncSpecimen(
        status="REJECTED", rejectionReason="LEAKING", rejectedAt=rejectedAt,
        medtechId=MEDTECH_USER_ID, patientName=encryptPii("Juan Dela Cruz"),
    )
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one=MagicMock(return_value=1)),
        MagicMock(all=MagicMock(return_value=[(specimen, None, rejectedAt)])),
    ])
    _useDb(db)
    try:
        response = await asyncClient.get(
            "/api/v1/results/medtech/history", params={"category": "REJECTED"}, headers=_headers()
        )
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total"], body["page"], body["pageSize"]) == (1, 1, 20)
    [item] = body["items"]
    assert item["patientUid"] == "PAT-000001"
    assert item["category"] == "REJECTED"
    assert item["rejectionReason"] == "LEAKING"
    assert "Juan" not in response.text and "gAAAAA" not in response.text
    rowsQuery = db.execute.await_args_list[1].args[0]
    assert MEDTECH_USER_ID in rowsQuery.compile().params.values()  # the caller, from the token
    db.commit.assert_awaited_once()  # the view's audit row


@pytest.mark.asyncio
async def test_aDeltaSyncReturnsTheRemovalLists(asyncClient: AsyncClient) -> None:
    gone = uuid.uuid4()
    db = makeSyncDb(removed=[(gone, None)])
    _useDb(db)
    try:
        response = await asyncClient.get(
            "/api/v1/sync/pull",
            params={"lastSyncedAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            headers=_headers(),
        )
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    changes = response.json()["changes"]
    assert changes["specimens"]["deleted"] == [str(gone)]
    assert {table: changes[table]["deleted"] for table in ("queueAssignments", "analysisResults", "manualOverrides")} == {
        "queueAssignments": [], "analysisResults": [], "manualOverrides": [],
    }
