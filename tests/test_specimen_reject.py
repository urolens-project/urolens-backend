"""Unit tests — specimen_service.rejectSpecimen (MedTech "Reject Specimen").

Covers the guard that keeps a rejection from stranding a result that has
already been confirmed and sent to the supervisor. The receiving-desk
rejection flow lives in receiveSpecimen and isn't covered here.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.core.exceptions import ConflictException, SpecimenNotFoundError, UnprocessableException
from src.models.analysis_result import ResultStatus
from src.models.specimen import Specimen
from src.services import specimen_service

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000051")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000052")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000053")


def _makeDb(
    status: str = "PROCESSING",
    medtechId: uuid.UUID = MEDTECH_ID,
    resultStatus: ResultStatus | None = None,
    exists: bool = True,
):
    """`resultStatus=None` means the specimen has no analysis result yet."""
    specimen = MagicMock(spec=Specimen)
    specimen.status = status
    specimen.medtechId = medtechId
    specimen.rejectionReason = None
    specimen.rejectionNote = None
    specimen.rejectedAt = None

    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = resultStatus

    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen if exists else None)
    db.execute = AsyncMock(return_value=executeResult)
    db.commit = AsyncMock()
    return db, specimen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resultStatus",
    [
        None,
        ResultStatus.PENDING_CONFIRM,
        ResultStatus.IMAGE_RETAKE_REQUESTED,
        ResultStatus.FAILED,
    ],
)
async def test_rejectSucceedsWhileResultIsStillWithTheMedtech(resultStatus):
    db, specimen = _makeDb(resultStatus=resultStatus)

    response = await specimen_service.rejectSpecimen(
        db, SPECIMEN_ID, MEDTECH_ID, "INSUFFICIENT_VOLUME", "Only 2 mL"
    )

    assert specimen.status == "REJECTED"
    assert specimen.rejectionReason == "INSUFFICIENT_VOLUME"
    assert specimen.rejectionNote == "Only 2 mL"
    assert specimen.rejectedAt is not None
    assert response.status == "REJECTED"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resultStatus",
    [
        ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        ResultStatus.RETURNED_FOR_CORRECTION,
        ResultStatus.CRITICAL_ESCALATED,
        ResultStatus.APPROVED,
        ResultStatus.RELEASED,
    ],
)
async def test_rejectIsBlockedOnceResultHasBeenSubmittedToTheSupervisor(resultStatus):
    """After confirmation, return or escalation the result belongs to the
    supervisor's workflow — rejecting the specimen now would leave it
    awaiting approval on a REJECTED specimen.
    """
    db, specimen = _makeDb(resultStatus=resultStatus)

    with pytest.raises(ConflictException) as exc:
        await specimen_service.rejectSpecimen(
            db, SPECIMEN_ID, MEDTECH_ID, "OTHER", None
        )

    assert exc.value.errorCode == "RESULT_ALREADY_SUBMITTED"
    assert specimen.status == "PROCESSING"
    assert specimen.rejectedAt is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejectStillConflictsOnAnAlreadyRejectedSpecimen():
    db, _ = _makeDb(status="REJECTED")

    with pytest.raises(ConflictException) as exc:
        await specimen_service.rejectSpecimen(db, SPECIMEN_ID, MEDTECH_ID, "OTHER", None)

    assert exc.value.errorCode == "SPECIMEN_ALREADY_REJECTED"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejectRefusesSpecimenAssignedToAnotherMedtech():
    db, specimen = _makeDb(medtechId=OTHER_MEDTECH_ID)

    with pytest.raises(HTTPException) as exc:
        await specimen_service.rejectSpecimen(db, SPECIMEN_ID, MEDTECH_ID, "OTHER", None)

    assert exc.value.status_code == 403
    assert exc.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    assert specimen.status == "PROCESSING"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejectRejectsAnUnknownReasonCodeBeforeTouchingTheDatabase():
    db, _ = _makeDb()

    with pytest.raises(UnprocessableException) as exc:
        await specimen_service.rejectSpecimen(db, SPECIMEN_ID, MEDTECH_ID, "BOGUS", None)

    assert exc.value.errorCode == "INVALID_REJECTION_REASON"
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejectRaisesNotFoundForUnknownSpecimen():
    db, _ = _makeDb(exists=False)

    with pytest.raises(SpecimenNotFoundError):
        await specimen_service.rejectSpecimen(db, SPECIMEN_ID, MEDTECH_ID, "OTHER", None)
