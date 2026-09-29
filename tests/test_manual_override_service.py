"""Unit tests — ManualOverrideService.overrideParameter (T2.6)

Tier-1 workflow (standards skill's named example: confirm -> override ->
approve -> release). Added as part of the results merge (plan row 6/7) to
close a coverage gap step 5a flagged: this behavior was traced by hand at
that step, never actually tested.

Covers:
- a client-supplied `originalAiValue` is ignored; the value re-derived from
  the stored `aiFindings` is what's persisted (original consolidation audit).
- who may override when (SEC-2, security audit F-03): nobody once APPROVED
  or RELEASED; a MedTech only on their own specimen while PENDING_CONFIRM or
  RETURNED_FOR_CORRECTION; a Supervisor only while PENDING_SUPERVISOR_APPROVAL.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from src.core.audit_logger import AuditLogger
from src.core.enums import UserRole
from src.core.exceptions import (
    ConflictException,
    ForbiddenException,
    UnprocessableException,
)
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.models.specimen import Specimen
from src.services.manual_override_service import ManualOverrideService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000021")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000022")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000023")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000024")


def _makeResult(status: str = ResultStatus.PENDING_CONFIRM) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.aiFindings = {"rbc_casts": 7.0, "uric_acid_crystals": 15.0}
    result.particleClasses = {}
    return result


def _makeSpecimen(medtechId: uuid.UUID = MEDTECH_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.medtechId = medtechId
    return specimen


def _makeDbMock(result: AnalysisResult, specimen: Specimen | None = None) -> AsyncMock:
    db = AsyncMock()
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=executeResult)
    db.get = AsyncMock(return_value=specimen if specimen is not None else _makeSpecimen())
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _makeService(db: AsyncMock) -> ManualOverrideService:
    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    return ManualOverrideService(db=db, auditLogger=auditLogger)


async def _override(
    service: ManualOverrideService,
    callerId: uuid.UUID = MEDTECH_ID,
    callerRole: str = UserRole.MEDTECH,
    parameter: str = "rbc_casts",
    originalAiValue: float | None = 1.0,
) -> ManualOverride:
    return await service.overrideParameter(
        resultId=RESULT_ID,
        parameter=parameter,
        correctedValue=3.0,
        rationale="Recount under supervision",
        originalAiValue=originalAiValue,
        medtechId=callerId,
        callerRole=callerRole,
        request=MagicMock(spec=Request),
    )


# ── Source of truth for the original value ────────────────────────────────────

@pytest.mark.asyncio
async def test_overrideIgnoresClientSuppliedOriginalAiValueAndUsesAiFindings():
    """A caller-supplied original_ai_value must never be trusted — the value
    actually persisted must come from the stored ai_findings instead.
    """
    db = _makeDbMock(_makeResult())
    service = _makeService(db)

    # Client claims the AI originally found 999 — the true stored value is 7.0.
    await _override(service, originalAiValue=999.0)

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ManualOverride)
    assert added.originalAiValue == "7.0"
    assert added.correctedValue == "3.0"
    detail = service.auditLogger.record.call_args[1]["detailJson"]
    assert detail["original_ai_value"] == 7.0


@pytest.mark.asyncio
async def test_overrideUnknownParameterRaisesWithoutTrustingClientValue():
    """If the parameter isn't in ai_findings at all, the override is rejected —
    the service never falls back to trusting the client's original_ai_value.
    """
    db = _makeDbMock(_makeResult())

    with pytest.raises(UnprocessableException) as excInfo:
        await _override(_makeService(db), parameter="nonexistent_parameter", originalAiValue=42.0)

    assert excInfo.value.errorCode == "PARAMETER_NOT_FOUND"
    db.add.assert_not_called()


# ── Finalised results (F-03) ──────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("finalStatus", [ResultStatus.APPROVED, ResultStatus.RELEASED])
@pytest.mark.parametrize(
    ("callerId", "callerRole"),
    [(MEDTECH_ID, UserRole.MEDTECH), (SUPERVISOR_ID, UserRole.SUPERVISOR)],
)
async def test_nobodyCanOverrideAnApprovedOrReleasedResult(finalStatus, callerId, callerRole):
    # RELEASED was overridable before SEC-2 — a released result could be
    # changed after the patient and physician had already seen it.
    result = _makeResult(status=finalStatus)
    db = _makeDbMock(result)

    with pytest.raises(UnprocessableException) as excInfo:
        await _override(_makeService(db), callerId=callerId, callerRole=callerRole)

    assert excInfo.value.errorCode == "RESULT_ALREADY_FINALISED"
    assert result.particleClasses == {}
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ── MedTech rules ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "editableStatus", [ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION]
)
async def test_medtechCanOverrideTheirOwnResultBeforeItIsSubmitted(editableStatus):
    db = _makeDbMock(_makeResult(status=editableStatus))

    await _override(_makeService(db))

    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lockedStatus",
    [
        ResultStatus.PENDING_SUPERVISOR_APPROVAL,
        ResultStatus.CRITICAL_ESCALATED,
        ResultStatus.IMAGE_RETAKE_REQUESTED,
        ResultStatus.FAILED,
    ],
)
async def test_medtechCannotOverrideOnceTheResultIsOutOfTheirHands(lockedStatus):
    db = _makeDbMock(_makeResult(status=lockedStatus))

    with pytest.raises(ConflictException) as excInfo:
        await _override(_makeService(db))

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_medtechCannotOverrideAnotherMedtechsSpecimen():
    db = _makeDbMock(_makeResult(), specimen=_makeSpecimen(medtechId=OTHER_MEDTECH_ID))

    with pytest.raises(ForbiddenException) as excInfo:
        await _override(_makeService(db))

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ── Supervisor rules ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supervisorCanOverrideDuringReviewWithoutBeingAssignedTheSpecimen():
    # Supervisors review every MedTech's work; the web review screen offers
    # override only for PENDING_SUPERVISOR_APPROVAL.
    db = _makeDbMock(
        _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL),
        specimen=_makeSpecimen(medtechId=OTHER_MEDTECH_ID),
    )

    await _override(_makeService(db), callerId=SUPERVISOR_ID, callerRole=UserRole.SUPERVISOR)

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.get.assert_not_awaited()  # no ownership lookup for a supervisor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notUnderReview",
    [
        ResultStatus.PENDING_CONFIRM,
        ResultStatus.RETURNED_FOR_CORRECTION,
        ResultStatus.CRITICAL_ESCALATED,
    ],
)
async def test_supervisorCanOverrideOnlyWhileTheResultIsUnderReview(notUnderReview):
    db = _makeDbMock(_makeResult(status=notUnderReview))

    with pytest.raises(ConflictException) as excInfo:
        await _override(_makeService(db), callerId=SUPERVISOR_ID, callerRole=UserRole.SUPERVISOR)

    assert excInfo.value.errorCode == "RESULT_NOT_EDITABLE"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_roleClaimIsComparedCaseInsensitively():
    # RequireRole matches roles case-insensitively; the service must agree.
    db = _makeDbMock(_makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL))

    await _override(_makeService(db), callerId=SUPERVISOR_ID, callerRole="supervisor")

    db.add.assert_called_once()
