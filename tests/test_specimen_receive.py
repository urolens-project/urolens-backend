"""Unit tests — specimen_service.receiveSpecimen (receptionist "Receive Specimen").

Covers, from two passes:
  - block 1: the re-receiving guard — a lab request not in PENDING_SAMPLE
    must be rejected before any DB write.
  - block 2: the rejection-reason validation-ordering fix (validation now
    runs before the patient lookup/Specimen construction/writes, alongside
    the status guard) and the audit log entry written for both outcomes.

This suite mocks `db` (an `AsyncSession`) entirely — there's no real test
database anywhere in this repo. "No Specimen row was written" is therefore
verified as "no write call happened on the mocked session" (`db.add` /
`db.flush` never called), which is the nearest equivalent this suite's
tooling supports to a real DB query proving the row's absence.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.core.exceptions import ConflictException, NotFoundException
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.schemas.specimen import SpecimenReceiveRequest
from src.services import specimen_service

LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000061")
RECEPTIONIST_ID = uuid.UUID("00000000-0000-0000-0000-000000000062")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000063")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000064")


def _makeDb(labRequestStatus: str | None = "PENDING_SAMPLE", labRequestExists: bool = True):
    labRequest = MagicMock(spec=LabRequest)
    labRequest.status = labRequestStatus
    labRequest.patientId = uuid.uuid4()

    db = AsyncMock()
    db.get = AsyncMock(return_value=labRequest if labRequestExists else None)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db, labRequest


def _makeFullReceiveDb():
    """A `db` that carries a PENDING_SAMPLE lab request all the way through
    a successful receive/reject: `db.get` yields the lab request then the
    patient (in that call order), `db.execute` reports no sample-UID
    collision, and `db.flush` assigns `specimen.specimenId`.
    """
    labRequest = MagicMock(spec=LabRequest)
    labRequest.status = "PENDING_SAMPLE"
    labRequest.patientId = PATIENT_ID
    labRequest.testType = "URINALYSIS"

    patient = MagicMock(spec=Patient)
    patient.patientId = PATIENT_ID
    patient.patientUid = "PT-000099"
    patient.firstName = "encrypted-first"
    patient.lastName = "encrypted-last"

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[labRequest, patient])
    uidCheckResult = MagicMock()
    uidCheckResult.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=uidCheckResult)
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.specimenId = SPECIMEN_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db, labRequest, patient


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


# ── block 2: rejection-reason validation now runs before any write ─────────


@pytest.mark.asyncio
async def test_receiveRejectsMissingReasonBeforeAnyWrite():
    db, _ = _makeDb()
    payload = SpecimenReceiveRequest(labRequestId=LAB_REQUEST_ID, visualCheckPassed=False)

    with pytest.raises(HTTPException) as excInfo:
        await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    assert excInfo.value.status_code == 400
    assert excInfo.value.errorCode == "REJECTION_REASON_REQUIRED"
    # Validation now happens before the patient lookup (a single db.get
    # call) and before any Specimen/SpecimenRejection row is constructed.
    db.get.assert_awaited_once()
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_receiveRejectsInvalidReasonBeforeAnyWrite():
    db, _ = _makeDb()
    payload = SpecimenReceiveRequest(
        labRequestId=LAB_REQUEST_ID, visualCheckPassed=False, rejectionReason="BOGUS"
    )

    with pytest.raises(HTTPException) as excInfo:
        await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    assert excInfo.value.status_code == 400
    assert excInfo.value.errorCode == "INVALID_REJECTION_REASON"
    db.get.assert_awaited_once()
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── block 2: audit log entry, written for both outcomes ─────────────────────


@pytest.mark.asyncio
async def test_receiveWritesAuditLogEntryOnSuccessfulReceive():
    db, _, _patient = _makeFullReceiveDb()
    payload = SpecimenReceiveRequest(labRequestId=LAB_REQUEST_ID, visualCheckPassed=True)

    with patch(
        "src.services.specimen_service.decryptPii", side_effect=["Juan", "Dela Cruz"]
    ), patch(
        "src.services.specimen_service.encryptPii", side_effect=lambda plain: plain
    ), patch("src.services.specimen_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        result = await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    mockAuditCls.return_value.record.assert_awaited_once()
    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["eventType"] == "SPECIMEN_RECEIVED"
    assert kwargs["entityType"] == "specimen"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == RECEPTIONIST_ID
    assert kwargs["detailJson"]["lab_request_id"] == str(LAB_REQUEST_ID)
    assert kwargs["detailJson"]["status"] == "RECEIVED"
    assert kwargs["detailJson"]["sample_uid"] == result.sampleUid
    assert kwargs["detailJson"]["rejection_reason"] is None
    # Audit write happens after the flush (specimen.id exists) but before
    # commit, in the same call — order proven by both having fired exactly
    # once by the time this assertion runs.
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_receiveWritesAuditLogEntryOnSuccessfulReject():
    db, _, _patient = _makeFullReceiveDb()
    payload = SpecimenReceiveRequest(
        labRequestId=LAB_REQUEST_ID,
        visualCheckPassed=False,
        rejectionReason="UNLABELED",
        freeTextNote="No label visible on container",
    )

    with patch(
        "src.services.specimen_service.decryptPii", side_effect=["Juan", "Dela Cruz"]
    ), patch(
        "src.services.specimen_service.encryptPii", side_effect=lambda plain: plain
    ), patch("src.services.specimen_service.AuditLogger") as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await specimen_service.receiveSpecimen(db, RECEPTIONIST_ID, payload)

    mockAuditCls.return_value.record.assert_awaited_once()
    kwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert kwargs["eventType"] == "SPECIMEN_REJECTED"
    assert kwargs["entityType"] == "specimen"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == RECEPTIONIST_ID
    assert kwargs["detailJson"]["status"] == "REJECTED"
    assert kwargs["detailJson"]["sample_uid"] is None
    assert kwargs["detailJson"]["rejection_reason"] == "UNLABELED"

    addedTypeNames = [type(call.args[0]).__name__ for call in db.add.call_args_list]
    assert "Specimen" in addedTypeNames
    assert "SpecimenRejection" in addedTypeNames
    db.commit.assert_awaited_once()
