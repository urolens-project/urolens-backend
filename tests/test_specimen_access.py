"""Unit tests — src.services.specimen_access (SEC-2).

The shared ownership check, the specimen row lock that serializes
specimen-scoped writes, and — per service — that state is re-read *after*
taking the lock. Each race test simulates a second device changing the
result between a service's first read and its lock, and asserts the service
acts on the fresh state. DB is mocked.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import UserRole
from src.core.exceptions import (
    ConflictError,
    ConflictException,
    ForbiddenException,
    SpecimenNotFoundError,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.image import Image
from src.models.specimen import Specimen
from src.services.ai_integration_service import AIIntegrationService
from src.services.image_retake_service import ImageRetakeService
from src.services.manual_override_service import ManualOverrideService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.specimen_access import getAssignedSpecimen

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000081")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000082")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000083")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000084")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-000000000085")


def _makeSpecimen(medtechId: uuid.UUID = MEDTECH_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    specimen.status = "PROCESSING"
    return specimen


def _makeResult(status: str) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.aiFindings = {"rbc_casts": 7.0}
    result.manualOverrides = []
    result.particleClasses = {}
    result.confirmedBy = None
    return result


def _executeReturning(*rows) -> AsyncMock:
    # Successive db.execute(...) calls return these rows via scalar_one_or_none().
    results = []
    for row in rows:
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = row
        results.append(executeResult)
    return AsyncMock(side_effect=results)


# ── getAssignedSpecimen ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_getAssignedSpecimenLocksTheSpecimenRow():
    db = AsyncMock()
    db.get = AsyncMock(return_value=_makeSpecimen())

    await getAssignedSpecimen(db, SPECIMEN_ID, MEDTECH_ID)

    db.get.assert_awaited_once_with(Specimen, SPECIMEN_ID, with_for_update=True)


@pytest.mark.asyncio
async def test_getAssignedSpecimenReturnsTheCallersOwnSpecimen():
    specimen = _makeSpecimen()
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)

    assert await getAssignedSpecimen(db, SPECIMEN_ID, MEDTECH_ID) is specimen


@pytest.mark.asyncio
@pytest.mark.parametrize("assignedTo", [OTHER_MEDTECH_ID, None])
async def test_getAssignedSpecimenForbidsAnyoneElseIncludingOnAnUnassignedSpecimen(assignedTo):
    db = AsyncMock()
    db.get = AsyncMock(return_value=_makeSpecimen(medtechId=assignedTo))

    with pytest.raises(ForbiddenException) as excInfo:
        await getAssignedSpecimen(db, SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_getAssignedSpecimenRaisesNotFoundForAMissingSpecimen():
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(SpecimenNotFoundError) as excInfo:
        await getAssignedSpecimen(db, SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.errorCode == "SPECIMEN_NOT_FOUND"


# ── Races: state is re-read under the lock ────────────────────────────────────

@pytest.mark.asyncio
async def test_confirmActsOnTheResultAsItIsAfterTheLockNotBefore():
    # First read: PENDING_CONFIRM. By the time the lock is held, another
    # device has confirmed it — this confirm must be refused, not re-applied.
    db = AsyncMock()
    db.get = AsyncMock(return_value=_makeSpecimen())
    db.execute = _executeReturning(
        _makeResult(ResultStatus.PENDING_CONFIRM),
        _makeResult(ResultStatus.PENDING_SUPERVISOR_APPROVAL),
    )
    service = ResultConfirmationService(
        db=db, auditLogger=MagicMock(), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=MagicMock())

    assert excInfo.value.errorCode == "RESULT_ALREADY_CONFIRMED"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_medtechOverrideActsOnTheResultAsItIsAfterTheLockNotBefore():
    db = AsyncMock()
    db.get = AsyncMock(return_value=_makeSpecimen())
    db.execute = _executeReturning(
        _makeResult(ResultStatus.PENDING_CONFIRM),
        _makeResult(ResultStatus.PENDING_SUPERVISOR_APPROVAL),
    )
    db.add = MagicMock()
    service = ManualOverrideService(db=db, auditLogger=MagicMock())

    with pytest.raises(ConflictException) as excInfo:
        await service.overrideParameter(
            resultId=RESULT_ID,
            parameter="rbc_casts",
            correctedValue=3.0,
            rationale="recount",
            originalAiValue=None,
            medtechId=MEDTECH_ID,
            callerRole=UserRole.MEDTECH,
            request=MagicMock(),
        )

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_uploadReadsTheResultOnlyAfterLockingTheSpecimen():
    # The check-then-reset window spans storage upload + inference, so the
    # result must be read under the lock.
    order: list[str] = []
    db = AsyncMock()

    async def _get(*args, **kwargs):
        order.append("lock specimen")
        return _makeSpecimen()

    async def _execute(*args, **kwargs):
        order.append("read result")
        executeResult = MagicMock()
        executeResult.scalar_one_or_none.return_value = None
        return executeResult

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(side_effect=_execute)

    await AIIntegrationService(db=db, auditLogger=MagicMock())._requireUploadAllowed(
        SPECIMEN_ID, MEDTECH_ID
    )

    assert order == ["lock specimen", "read result"]


@pytest.mark.asyncio
async def test_discardReChecksTheImageAfterTheLockSoTwoDiscardsCannotBothPass():
    image = MagicMock(spec=Image)
    image.imageId = IMAGE_ID
    image.specimenId = SPECIMEN_ID
    image.status = "ACTIVE"

    async def _get(model, recordId, **options):
        return {Image: image, Specimen: _makeSpecimen()}[model]

    async def _refresh(obj):
        obj.status = "DISCARDED"  # the other request committed first

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock(side_effect=_refresh)
    service = ImageRetakeService(db=db, auditLogger=MagicMock())

    with pytest.raises(ConflictError) as excInfo:
        await service.discardAndRetake(imageId=IMAGE_ID, medtechId=MEDTECH_ID)

    assert "already been discarded" in excInfo.value.detail
    db.commit.assert_not_awaited()
