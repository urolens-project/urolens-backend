"""HTTP-level tests — `POST /specimens/{id}/reject` (UROLENS-238).

Through the real route and service (DB mocked): MedTech-only, the note is
capped at 500 characters, a returned result's specimen can now be rejected,
the rejection is audited and receptionists are notified.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from httpx import AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import ResultStatus
from src.models.audit_log import AuditLog
from src.models.specimen import Specimen
from tests.integration.conftest import MEDTECH_USER_ID, TEST_SPECIMEN_ID


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


def _useDb(resultStatus: ResultStatus | None) -> tuple[AsyncMock, Specimen]:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = TEST_SPECIMEN_ID
    specimen.sampleUid = "SMP-20260930-00099"
    specimen.medtechId = MEDTECH_USER_ID
    specimen.status = "PROCESSING"
    db = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock(return_value=specimen)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=resultStatus)))

    async def _override() -> AsyncIterator[AsyncMock]:
        yield db

    app.dependency_overrides[getDb] = _override
    return db, specimen


async def _post(asyncClient: AsyncClient, body: dict, role: str = "MEDTECH") -> object:
    return await asyncClient.post(f"/api/v1/specimens/{TEST_SPECIMEN_ID}/reject", json=body, headers=_headers(role))


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["SUPERVISOR", "RECEPTIONIST", "PHYSICIAN", "PATIENT"])
async def test_onlyAMedtechCanRejectAnAssignedSpecimen(asyncClient: AsyncClient, role: str) -> None:
    response = await _post(asyncClient, {"reasonCode": "OTHER"}, role)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_aNoteOverFiveHundredCharactersIsRefused(asyncClient: AsyncClient) -> None:
    response = await _post(asyncClient, {"reasonCode": "OTHER", "freeTextNote": "x" * 501})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_aReturnedResultsSpecimenIsRejectedAuditedAndReceptionistsAreTold(asyncClient: AsyncClient) -> None:
    db, specimen = _useDb(ResultStatus.RETURNED_FOR_CORRECTION)
    notify = AsyncMock()
    try:
        with patch("src.services.specimen_service.NotificationService") as notificationService:
            notificationService.return_value.notifyReceptionistsSpecimenRejected = notify
            response = await _post(asyncClient, {"reasonCode": "UNLABELED", "freeTextNote": "  No patient label  "})
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "REJECTED" and body["rejectedAt"].endswith("+00:00")
    assert (specimen.status, specimen.rejectionNote) == ("REJECTED", "No patient label")
    [audit] = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]
    assert audit.eventType == "SPECIMEN_REJECTED"
    notify.assert_awaited_once_with(specimenId=TEST_SPECIMEN_ID, sampleUid="SMP-20260930-00099", reason="unlabeled")


@pytest.mark.asyncio
async def test_aSubmittedResultsSpecimenStillCannotBeRejected(asyncClient: AsyncClient) -> None:
    db, specimen = _useDb(ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    try:
        response = await _post(asyncClient, {"reasonCode": "OTHER"})
    finally:
        app.dependency_overrides.pop(getDb, None)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESULT_ALREADY_SUBMITTED"
    assert specimen.status == "PROCESSING"
    db.commit.assert_not_awaited()
