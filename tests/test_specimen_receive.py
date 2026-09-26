"""Unit tests — specimen_service.receiveSpecimen (receptionist "Receive Specimen").

Covers the re-receiving guard: a lab request not in PENDING_SAMPLE must be
rejected before any DB write. The visual-check-passed/rejection-reason paths
aren't covered here — see the rejection-reason validation-ordering fix (a
separate, already-known pass) for that logic.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import ConflictException, NotFoundException
from src.models.lab_request import LabRequest
from src.schemas.specimen import SpecimenReceiveRequest
from src.services import specimen_service

LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000061")
RECEPTIONIST_ID = uuid.UUID("00000000-0000-0000-0000-000000000062")


def _makeDb(labRequestStatus: str | None = "PENDING_SAMPLE", labRequestExists: bool = True):
    labRequest = MagicMock(spec=LabRequest)
    labRequest.status = labRequestStatus
    labRequest.patientId = uuid.uuid4()

    db = AsyncMock()
    db.get = AsyncMock(return_value=labRequest if labRequestExists else None)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db, labRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labRequestStatus",
    ["SAMPLE_RECEIVED", "IN_PROGRESS", "COMPLETED", "REJECTED"],
)
async def test_receiveConflictsWhenLabRequestIsNotPendingSample(labRequestStatus):
    db, _ = _makeDb(labRequestStatus=labRequestStatus)
    payload = SpecimenReceiveRequest(labRequestId=LAB_REQUEST_ID, visualCheckPassed=True)

    with pytest.raises(ConflictException) as excInfo:
        await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    assert excInfo.value.errorCode == "SPECIMEN_ALREADY_RECEIVED"
    # The guard fires before any DB write, and before the second db.get()
    # (the patient lookup) — a single db.get call, no add/commit.
    db.get.assert_awaited_once()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_receiveRaisesNotFoundForUnknownLabRequest():
    db, _ = _makeDb(labRequestExists=False)
    payload = SpecimenReceiveRequest(labRequestId=LAB_REQUEST_ID, visualCheckPassed=True)

    with pytest.raises(NotFoundException) as excInfo:
        await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    assert excInfo.value.errorCode == "LAB_REQUEST_NOT_FOUND"
    db.add.assert_not_called()
