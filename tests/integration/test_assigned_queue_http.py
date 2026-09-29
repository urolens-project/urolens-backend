"""HTTP-level tests — the MedTech queue routes (UROLENS-225).

Through the real routes: `GET /sync/pull` is MedTech-only and returns the patient
code but never the name; `GET /results/medtech/pending` validates its new `status` /
`sort` filters and passes them to the service.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from main import app
from src.api.results import getConfirmationService
from src.core.config import settings
from src.core.database import getDb
from src.core.encryption import encryptPii
from tests.conftest import makeSyncDb
from tests.integration.conftest import MEDTECH_USER_ID


def _token(role: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "user_id": str(uuid.uuid4() if role != "MEDTECH" else MEDTECH_USER_ID),
            "role": role,
            "session_id": str(uuid.uuid4()),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwtSigningKey,
        algorithm=settings.jwtAlgorithm,
    )


def _syncSupabase(specimenRows: list[dict]) -> MagicMock:
    def _table(name: str) -> MagicMock:
        query = MagicMock()
        for method in ("select", "eq", "gt", "in_"):
            getattr(query, method).return_value = query
        query.execute = AsyncMock(return_value=MagicMock(data=specimenRows if name == "specimens" else []))
        return query

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["SUPERVISOR", "RECEPTIONIST", "PHYSICIAN", "PATIENT"])
async def test_syncIsForbiddenToEveryRoleButMedtech(asyncClient, role: str) -> None:
    response = await asyncClient.get("/api/v1/sync/pull", headers={"Authorization": f"Bearer {_token(role)}"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_medtechSyncReturnsThePatientCodeButNeverTheName(asyncClient) -> None:
    db = makeSyncDb()

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        with patch("src.services.sync_service.supabase", _syncSupabase(
            [{"specimen_id": str(uuid.uuid4()), "patient_uid": "PAT-000001", "patient_name": encryptPii("Juan Dela Cruz")}]
        )):
            response = await asyncClient.get(
                "/api/v1/sync/pull", headers={"Authorization": f"Bearer {_token('MEDTECH')}"}
            )
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    [specimen] = response.json()["changes"]["specimens"]["created"]
    assert specimen["patient_uid"] == "PAT-000001"
    assert specimen["patient_name"] == ""
    assert "Juan" not in response.text and "gAAAAA" not in response.text


@pytest.mark.asyncio
async def test_pendingRejectsAnUnknownStatusFilter(asyncClient) -> None:
    response = await asyncClient.get(
        "/api/v1/results/medtech/pending",
        params={"status": "APPROVED"},
        headers={"Authorization": f"Bearer {_token('MEDTECH')}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_pendingPassesItsFiltersToTheService(asyncClient) -> None:
    service = MagicMock()
    service.listPendingForMedtech = AsyncMock(return_value={"items": [], "total": 0, "page": 2, "pageSize": 10})
    app.dependency_overrides[getConfirmationService] = lambda: service
    try:
        response = await asyncClient.get(
            "/api/v1/results/medtech/pending",
            params={"status": "RETURNED_FOR_CORRECTION", "sort": "newest", "page": 2, "pageSize": 10},
            headers={"Authorization": f"Bearer {_token('MEDTECH')}"},
        )
    finally:
        app.dependency_overrides.pop(getConfirmationService, None)

    assert response.status_code == 200, response.text
    kwargs = service.listPendingForMedtech.await_args.kwargs
    assert (kwargs["status"], kwargs["sort"], kwargs["page"], kwargs["pageSize"]) == (
        "RETURNED_FOR_CORRECTION", "newest", 2, 10,
    )
    assert kwargs["medtechId"] == MEDTECH_USER_ID
