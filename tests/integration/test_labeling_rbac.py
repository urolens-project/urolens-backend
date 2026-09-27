"""Integration tests — RBAC on the Sample Labeling endpoints (UROLENS-142
"fix intake role alignment on labeling.py").

`src/api/labeling.py` was gated `RequireRole([UserRole.MEDTECH])`; corrected
to `RequireRole([UserRole.RECEPTIONIST])` per UROLENS-141's UAC. This file
proves the role check itself at the real HTTP layer (a unit test on the
service function can't exercise route-level RBAC at all):
  - MEDTECH, SUPERVISOR, ADMINISTRATOR now get 403 on all three routes.
  - RECEPTIONIST still gets past the role check (mocked far enough to reach
    a real 2xx, not just "not 403") — a regression check that the fix didn't
    also break the intended caller.

Only three routes exist on this branch — `POST /{id}/label/print` is
UROLENS-141 block-1 work that hasn't been merged/rebased into this branch,
so there's nothing to gate there yet (see the report for this pass).
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
from src.models.specimen import Specimen

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000120")
LABEL_ID = uuid.UUID("00000000-0000-0000-0000-000000000121")
PRINT_JOB_ID = uuid.UUID("00000000-0000-0000-0000-000000000122")

_BLOCKED_ROLES = ["MEDTECH", "SUPERVISOR", "ADMINISTRATOR"]


def _mintToken(role: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(uuid.uuid4()),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


@pytest_asyncio.fixture
async def asyncClient():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _overrideDb(db):
    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override


def _clearDbOverride():
    app.dependency_overrides.pop(getDb, None)


def _makeReceivedSpecimen() -> MagicMock:
    specimen = MagicMock(spec=Specimen)
    specimen.status = "RECEIVED"
    specimen.patientName = "encrypted-name"
    specimen.patientUid = "PAT-000001"
    specimen.sampleUid = "SMP-000001"
    specimen.testType = "URINALYSIS"
    return specimen


# ── blocked roles: 403 on all three routes ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", _BLOCKED_ROLES)
async def test_searchReceivedRejectsNonReceptionistRoles(asyncClient, role):
    db = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken(role)
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/specimens/search-received",
                params={"q": "PAT"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", _BLOCKED_ROLES)
async def test_generateLabelRejectsNonReceptionistRoles(asyncClient, role):
    db = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken(role)
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/specimens/{SPECIMEN_ID}/label",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    db.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", _BLOCKED_ROLES)
async def test_confirmLabelAffixedRejectsNonReceptionistRoles(asyncClient, role):
    db = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken(role)
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/specimens/{SPECIMEN_ID}/label/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={"offlineOverride": False},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    db.execute.assert_not_called()


# ── Receptionist: still gets past the role check on all three routes ───────


@pytest.mark.asyncio
async def test_searchReceivedSucceedsForReceptionist(asyncClient):
    db = AsyncMock()
    emptyResult = MagicMock()
    emptyResult.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=emptyResult)
    _overrideDb(db)
    try:
        token = _mintToken("RECEPTIONIST")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.get(
                "/api/v1/specimens/search-received",
                params={"q": "PAT"},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_generateLabelSucceedsForReceptionist(asyncClient):
    specimen = _makeReceivedSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            if not hasattr(obj, "labelId") or obj.labelId is None:
                obj.labelId = LABEL_ID
            if not hasattr(obj, "printJobId") or obj.printJobId is None:
                obj.printJobId = PRINT_JOB_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken("RECEPTIONIST")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)), patch(
            "src.services.labeling_service.decryptPii", return_value="Juan Dela Cruz"
        ):
            response = await asyncClient.post(
                f"/api/v1/specimens/{SPECIMEN_ID}/label",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 201
    assert response.json()["preview"]["patientName"] == "Juan Dela Cruz"


@pytest.mark.asyncio
async def test_confirmLabelAffixedSucceedsForReceptionist(asyncClient):
    specimen = _makeReceivedSpecimen()
    label = MagicMock()
    label.labelId = LABEL_ID
    label.affixedConfirmed = False
    label.affixedAt = None

    db = AsyncMock()
    labelQueryResult = MagicMock()
    labelQueryResult.scalars.return_value.first.return_value = label
    db.execute = AsyncMock(return_value=labelQueryResult)
    db.get = AsyncMock(return_value=specimen)
    db.commit = AsyncMock()
    _overrideDb(db)
    try:
        token = _mintToken("RECEPTIONIST")
        with patch("src.core.rbac.isSessionActive", AsyncMock(return_value=True)):
            response = await asyncClient.post(
                f"/api/v1/specimens/{SPECIMEN_ID}/label/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={"offlineOverride": False},
            )
    finally:
        _clearDbOverride()

    assert response.status_code == 200
    assert response.json()["updatedStatus"] == "LABELED"
