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
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import (
    ConflictException,
    ForbiddenException,
    NotFoundException,
    SpecimenNotFoundError,
    UnprocessableException,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import Image, ImageStatus
from src.models.result_confirmation import ResultConfirmation
from src.models.specimen import Specimen
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.smart_diagnosis_service import SmartDiagnosisService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000043")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000044")


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
    result.imageId = IMAGE_ID
    return result


def _makeImage(status: str = ImageStatus.ACTIVE) -> Image:
    image = MagicMock(spec=Image)
    image.imageId = IMAGE_ID
    image.status = status
    return image


def _getByModel(specimen: Specimen | None, image: Image | None = None) -> Callable:
    # `db.get(Model, id)`: the specimen for Specimen, the result's image for Image.
    async def _get(model: type, *args: object, **kwargs: object) -> object:
        return (image if image is not None else _makeImage()) if model is Image else specimen
    return _get


def _makeSpecimen(status: str = "PROCESSING", medtechId: uuid.UUID = MEDTECH_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = status
    specimen.medtechId = medtechId
    return specimen


def _answerByEntity(
    result: AnalysisResult | None, existingConfirmation: ResultConfirmation | None = None
) -> Callable[[Select], MagicMock]:
    # Like a real session: a result query returns the result; the "existing
    # confirmation?" lookup returns the confirmation row (None by default).
    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = (
            existingConfirmation if entity is ResultConfirmation else result
        )
        return executeResult
    return _execute


def _makeDbMock(getResultReturn, specimen: Specimen | None = None) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(side_effect=_getByModel(specimen if specimen is not None else _makeSpecimen()))
    db.execute = AsyncMock(side_effect=_answerByEntity(getResultReturn))
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
        ResultStatus.CRITICAL_ESCALATED,
    ],
)
@pytest.mark.asyncio
async def test_confirmAlreadyConfirmedStatusRaisesConflict(status):
    """Every status that means the medtech-confirmation step already
    happened must reject a second confirm — not just PENDING_SUPERVISOR_APPROVAL.

    RETURNED_FOR_CORRECTION is deliberately excluded: it's a resubmit, not a
    double-confirm — see test_confirmReturnedForCorrectionResubmitsSuccessfully.
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
async def test_confirmReturnedForCorrectionResubmitsSuccessfully():
    """A result a supervisor sent back (RETURNED_FOR_CORRECTION) is one of
    the two confirmable statuses — confirming it again resubmits it for
    supervisor approval, same as the first confirm.
    """
    result = _makeResult(status=ResultStatus.RETURNED_FOR_CORRECTION, aiFindings={"RBC": 12, "WBC": 4})
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert result.confirmedBy == MEDTECH_ID
    assert result.confirmedAt is not None
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "resultStatus",
    [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION],
)
@pytest.mark.asyncio
async def test_confirmIsBlockedWhenSpecimenWasRejected(resultStatus):
    """A rejected specimen's result must never reach the supervisor's queue —
    even from a confirmable status (e.g. a queued offline confirm replayed
    after the MedTech rejected the specimen).
    """
    result = _makeResult(status=resultStatus)
    db = _makeDbMock(getResultReturn=result)
    db.get = AsyncMock(side_effect=_getByModel(_makeSpecimen(status="REJECTED")))
    service = _makeService(db)

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"
    assert result.status == resultStatus
    assert result.confirmedBy is None
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


@pytest.mark.asyncio
async def test_confirmKeepsTheLatestOverrideOfAParameterCorrectedTwice() -> None:
    # `manualOverrides` loads oldest first (UROLENS-227), so the latest wins.
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM, aiFindings={"RBC": 12})
    first, latest = MagicMock(), MagicMock()
    first.parameterName, first.correctedValue = "RBC", "15.0"
    latest.parameterName, latest.correctedValue = "RBC", "9.0"
    result.manualOverrides = [first, latest]
    service = _makeService(_makeDbMock(getResultReturn=result))

    await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert result.particleClasses == {"RBC": 9.0}


# ── Ownership (SEC-2, security audit F-08) ────────────────────────────────────

@pytest.mark.asyncio
async def test_confirmIsForbiddenForAMedtechTheSpecimenIsNotAssignedTo():
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM)
    db = _makeDbMock(getResultReturn=result, specimen=_makeSpecimen(medtechId=OTHER_MEDTECH_ID))
    service = _makeService(db)

    with pytest.raises(ForbiddenException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    assert result.status == ResultStatus.PENDING_CONFIRM
    assert result.confirmedBy is None
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmOwnershipIsCheckedBeforeStatusSoANonOwnerLearnsNothing():
    # An already-approved result must still answer a non-owner with 403,
    # not reveal its state through RESULT_ALREADY_CONFIRMED.
    result = _makeResult(status=ResultStatus.APPROVED)
    db = _makeDbMock(getResultReturn=result, specimen=_makeSpecimen(medtechId=OTHER_MEDTECH_ID))

    with pytest.raises(ForbiddenException):
        await _makeService(db).confirmResult(
            resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock()
        )


@pytest.mark.asyncio
async def test_confirmRaisesNotFoundWhenTheResultsSpecimenIsMissing():
    result = _makeResult(status=ResultStatus.PENDING_CONFIRM)
    db = _makeDbMock(getResultReturn=result)
    db.get = AsyncMock(return_value=None)

    with pytest.raises(SpecimenNotFoundError) as excInfo:
        await _makeService(db).confirmResult(
            resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock()
        )

    assert excInfo.value.errorCode == "SPECIMEN_NOT_FOUND"
    db.commit.assert_not_awaited()


# ── UROLENS-226: confirm response and non-confirmable statuses ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "resubmitted"),
    [(ResultStatus.PENDING_CONFIRM, False), (ResultStatus.RETURNED_FOR_CORRECTION, True)],
)
async def test_confirmResponseSaysWhetherItWasAResubmitAndTheNewStatus(
    status: ResultStatus, resubmitted: bool
) -> None:
    result = _makeResult(status=status)
    service = _makeService(_makeDbMock(getResultReturn=result))

    response = await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert response.resultId == RESULT_ID
    assert response.confirmedBy == MEDTECH_ID
    assert response.resubmitted is resubmitted
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert response.id is not None


@pytest.mark.asyncio
async def test_confirmAFailedResultIsNotConfirmableRatherThanAlreadyConfirmed() -> None:
    """The app treats RESULT_ALREADY_CONFIRMED as success, so a FAILED result
    must get its own code instead of being silently marked done offline.
    """
    result = _makeResult(status=ResultStatus.FAILED)
    db = _makeDbMock(getResultReturn=result)
    service = _makeService(db)

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=_requestMock())

    assert excInfo.value.status_code == 409
    assert excInfo.value.errorCode == "RESULT_NOT_CONFIRMABLE"
    assert result.status == ResultStatus.FAILED
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
