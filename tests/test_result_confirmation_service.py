"""Unit tests — ResultConfirmationService (T2.5, plan doc row 6).

tests/integration/test_smart_diagnosis.py already covers the Smart
Diagnosis wiring (`test_confirmResultTriggersSmartDiagnosis`,
`test_confirmResultSucceedsEvenWhenSmartDiagnosisFails`), but both of those
tests patch out `_getResult`/`_validateNoPendingRetake` entirely — so
`confirmResult`'s own guard logic (not-found, already-confirmed, pending
image retake) has never actually been exercised. That's the gap this file
closes, following the same direct-service-construction pattern established
in tests/test_manual_override_service.py and tests/test_result_review_service.py.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.smart_diagnosis_service import SmartDiagnosisService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")


def _makeResult(
    status: str = ResultStatus.PENDING_CONFIRM,
    aiFindings: dict | None = None,
) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.aiFindings = aiFindings or {"RBC": 12}
    result.manualOverrides = []
    result.particleClasses = None
    result.confirmedBy = None
    result.confirmedAt = None
    return result


def _makeDbMock(getResultReturn) -> AsyncMock:
    db = AsyncMock()
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = getResultReturn
    db.execute = AsyncMock(return_value=executeResult)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _makeService(db: AsyncMock) -> ResultConfirmationService:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    _notif = MagicMock(spec=NotificationService)
    _notif.notifySupervisorResultReady = AsyncMock()
    _smartDiag = MagicMock(spec=SmartDiagnosisService)
    _smartDiag.run = AsyncMock(return_value=None)
    return ResultConfirmationService(
        db=db,
        auditLogger=auditLogger,
        _smartDiagnosisService=_smartDiag,
        _notifService=_notif,
    )


def _requestMock() -> MagicMock:
    request = MagicMock()
    request.client = None
    return request


@pytest.mark.asyncio
async def test_confirmNonexistentResultRaisesNotFound():
    db = _makeDbMock(getResultReturn=None)
    service = _makeService(db)

    with pytest.raises(NotFoundException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [
        ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        ResultStatus.APPROVED,
        ResultStatus.RELEASED,
        ResultStatus.RETURNED_FOR_CORRECTION,
        ResultStatus.CRITICAL_ESCALATED,
    ],
)
@pytest.mark.asyncio
async def test_confirmAlreadyConfirmedStatusRaisesConflict(status):
    """Every status that means the medtech-confirmation step already
    happened must reject a second confirm — not just PENDING_SUPERVISOR_APPROVAL.
    """
    result = _makeResult(status=status)
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.errorCode == "RESULT_ALREADY_CONFIRMED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmPendingImageRetakeBlocksConfirmation():
    result = _makeResult(status=ResultStatus.IMAGE_RETAKE_REQUESTED)
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    with pytest.raises(UnprocessableException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.errorCode == "PENDING_RETAKE"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_confirmConcurrentDoubleSubmitRaisesConflictNotIntegrityError():
    """A concurrent double-submit that races past the status guard and hits
    the DB's unique constraint on flush() must surface as the same
    ConflictException as a sequential re-confirm — not a raw IntegrityError.
    """
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM)
    db = _makeDbMock(getResultReturn=result)
    db.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate key")))
    service = _makeService(db)

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.errorCode == "RESULT_ALREADY_CONFIRMED"


@pytest.mark.asyncio
async def test_confirmHappyPathTransitionsStatusAndSettlesParticleClasses():
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM, aiFindings={"RBC": 12, "WBC": 4})
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert result.confirmedBy == MEDTECH_ID
    assert result.confirmedAt is not None
    # No manual overrides on this result -> particle_classes is a straight copy of ai_findings
    assert result.particleClasses == {"RBC": 12, "WBC": 4}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmMergesManualOverridesIntoParticleClasses():
    """particle_classes must reflect MedTech corrections, not the raw AI
    findings, for any parameter that was overridden.
    """
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM, aiFindings={"RBC": 12, "WBC": 4})
    override = MagicMock()
    override.parameterName = "RBC"
    override.correctedValue = "15"
    result.manualOverrides = [override]
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert result.particleClasses == {"RBC": 15.0, "WBC": 4}
