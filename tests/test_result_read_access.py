"""Unit tests — MedTech read access to results (UROLENS-222, security audit F-22).

A MedTech may read a result (detail, Smart Diagnosis) only for a specimen
assigned to them; supervisors read every result. The refusal happens before
any patient data is decrypted or the view is logged. DB is mocked.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import UserRole
from src.core.exceptions import ForbiddenException, NotFoundException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.specimen import Specimen
from src.services import result_review_service
from src.services.result_review_service import ResultReviewService
from src.services.specimen_access import requireResultReadable

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d3")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d4")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d5")


def _makeResult() -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_SUPERVISOR_APPROVAL
    return result


def _makeSpecimen(medtechId: uuid.UUID = MEDTECH_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    specimen.patientUid = None
    specimen.patientName = "ciphertext"
    return specimen


def _makeDb(result=None, specimen=None) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[result, specimen])
    return db


# ── requireResultReadable (used by GET /results/{id}/smart-diagnosis) ─────────

@pytest.mark.asyncio
async def test_medtechCanReadTheirOwnSpecimensResult():
    await requireResultReadable(
        _makeDb(_makeResult(), _makeSpecimen()), RESULT_ID, MEDTECH_ID, UserRole.MEDTECH
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("assignedTo", [OTHER_MEDTECH_ID, None])
async def test_medtechCannotReadAnotherMedtechsOrAnUnassignedResult(assignedTo):
    with pytest.raises(ForbiddenException) as excInfo:
        await requireResultReadable(
            _makeDb(_makeResult(), _makeSpecimen(medtechId=assignedTo)),
            RESULT_ID, MEDTECH_ID, UserRole.MEDTECH,
        )

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_medtechGetsNotFoundForAMissingResult():
    with pytest.raises(NotFoundException) as excInfo:
        await requireResultReadable(_makeDb(None, None), RESULT_ID, MEDTECH_ID, UserRole.MEDTECH)

    assert excInfo.value.errorCode == "RESULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_resultWhoseSpecimenIsGoneIsForbiddenToAMedtech():
    with pytest.raises(ForbiddenException):
        await requireResultReadable(_makeDb(_makeResult(), None), RESULT_ID, MEDTECH_ID, UserRole.MEDTECH)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, "supervisor"])
async def test_supervisorReadsAnyResultWithoutAnOwnershipLookup(role):
    db = _makeDb()

    await requireResultReadable(db, RESULT_ID, SUPERVISOR_ID, role)

    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_roleClaimIsComparedCaseInsensitively():
    with pytest.raises(ForbiddenException):
        await requireResultReadable(
            _makeDb(_makeResult(), _makeSpecimen(medtechId=OTHER_MEDTECH_ID)),
            RESULT_ID, MEDTECH_ID, "medtech",
        )


# ── getFullResult (GET /results/{id}) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_resultDetailRefusesAnotherMedtechBeforeDecryptingOrLogging():
    db = _makeDb(_makeResult(), _makeSpecimen(medtechId=OTHER_MEDTECH_ID))
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    service = ResultReviewService(db=db, auditLogger=auditLogger)

    with patch.object(result_review_service, "_decryptOrNone") as decryptSpy, \
         pytest.raises(ForbiddenException) as excInfo:
        await service.getFullResult(
            RESULT_ID, viewerId=MEDTECH_ID, viewerRole=UserRole.MEDTECH
        )

    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    decryptSpy.assert_not_called()
    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()
