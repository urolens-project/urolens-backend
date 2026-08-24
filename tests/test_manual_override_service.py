"""Unit tests — ManualOverrideService.override_parameter (T2.6)

Tier-1 workflow (standards skill's named example: confirm -> override ->
approve -> release). Added as part of the results merge (plan row 6/7) to
close a coverage gap step 5a flagged: this behavior was traced by hand at
that step, never actually tested.

Covers the one behavior the original consolidation audit called out
specifically: a client-supplied `original_ai_value` must be ignored, and
the value re-derived from the stored `ai_findings` must be what's
persisted.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request

from src.core.audit_logger import AuditLogger
from src.core.exceptions import UnprocessableException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.manual_override import ManualOverride
from src.services.manual_override_service import ManualOverrideService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000021")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000022")


def _makeResult(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    result.aiFindings = {"rbc_casts": 7.0, "uric_acid_crystals": 15.0}
    return result


def _makeDbMock(result: AnalysisResult) -> AsyncMock:
    db = AsyncMock()
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=executeResult)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_overrideIgnoresClientSuppliedOriginalAiValueAndUsesAiFindings():
    """A caller-supplied original_ai_value must never be trusted — the value
    actually persisted must come from the stored ai_findings instead.
    """
    result = _makeResult()
    db = _makeDbMock(result)
    auditLogger = MagicMock(spec=AuditLogger)
    auditLogger.record = AsyncMock()
    request = MagicMock(spec=Request)

    _service = ManualOverrideService(db=db, auditLogger=auditLogger)

    # Client claims the AI originally found 999 — the true stored value is 7.0.
    await _service.overrideParameter(
        resultId=RESULT_ID,
        parameter="rbc_casts",
        correctedValue=3.0,
        rationale="Recount under supervision",
        originalAiValue=999.0,
        medtechId=MEDTECH_ID,
        request=request,
    )

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ManualOverride)
    # The persisted value is the re-derived one (7.0), never the client's claim (999.0).
    assert added.originalAiValue == "7.0"
    assert added.correctedValue == "3.0"

    # Same value shows up in the audit trail, not the client-supplied one.
    auditLogger.record.assert_called_once()
    detail = auditLogger.record.call_args[1]["detailJson"]
    assert detail["original_ai_value"] == 7.0


@pytest.mark.asyncio
async def test_overrideUnknownParameterRaisesWithoutTrustingClientValue():
    """If the parameter isn't in ai_findings at all, the override is rejected —
    the service never falls back to trusting the client's original_ai_value.
    """
    result = _makeResult()
    db = _makeDbMock(result)
    auditLogger = MagicMock(spec=AuditLogger)
    request = MagicMock(spec=Request)

    _service = ManualOverrideService(db=db, auditLogger=auditLogger)

    with pytest.raises(UnprocessableException) as excInfo:
        await _service.overrideParameter(
            resultId=RESULT_ID,
            parameter="nonexistent_parameter",
            correctedValue=1.0,
            rationale="test",
            originalAiValue=42.0,
            medtechId=MEDTECH_ID,
            request=request,
        )
    assert excInfo.value.errorCode == "PARAMETER_NOT_FOUND"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_overrideRejectedAfterResultFinalised():
    """Cannot override a parameter once the result has been approved or
    returned for correction.
    """
    result = _makeResult(status=ResultStatus.APPROVED)
    db = _makeDbMock(result)
    auditLogger = MagicMock(spec=AuditLogger)
    request = MagicMock(spec=Request)

    _service = ManualOverrideService(db=db, auditLogger=auditLogger)

    with pytest.raises(UnprocessableException) as excInfo:
        await _service.overrideParameter(
            resultId=RESULT_ID,
            parameter="rbc_casts",
            correctedValue=1.0,
            rationale="test",
            originalAiValue=1.0,
            medtechId=MEDTECH_ID,
            request=request,
        )
    assert excInfo.value.errorCode == "RESULT_ALREADY_FINALISED"
    db.add.assert_not_called()
