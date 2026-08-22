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

from src.urolens.core.audit_logger import AuditLogger
from src.urolens.core.exceptions import UnprocessableException
from src.urolens.models.analysis_result import AnalysisResult, ResultStatus
from src.urolens.models.manual_override import ManualOverride
from src.urolens.services.manual_override_service import ManualOverrideService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000021")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000022")


def _make_result(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.result_id = RESULT_ID
    result.specimen_id = SPECIMEN_ID
    result.status = status
    result.ai_findings = {"rbc_casts": 7.0, "uric_acid_crystals": 15.0}
    return result


def _make_db_mock(result: AnalysisResult) -> AsyncMock:
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=execute_result)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_override_ignores_client_supplied_original_ai_value_and_uses_ai_findings():
    """A caller-supplied original_ai_value must never be trusted — the value
    actually persisted must come from the stored ai_findings instead.
    """
    result = _make_result()
    db = _make_db_mock(result)
    audit_logger = MagicMock(spec=AuditLogger)
    audit_logger.record = AsyncMock()
    request = MagicMock(spec=Request)

    service = ManualOverrideService(db=db, audit_logger=audit_logger)

    # Client claims the AI originally found 999 — the true stored value is 7.0.
    await service.override_parameter(
        result_id=RESULT_ID,
        parameter="rbc_casts",
        corrected_value=3.0,
        rationale="Recount under supervision",
        original_ai_value=999.0,
        medtech_id=MEDTECH_ID,
        request=request,
    )

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ManualOverride)
    # The persisted value is the re-derived one (7.0), never the client's claim (999.0).
    assert added.original_ai_value == "7.0"
    assert added.corrected_value == "3.0"

    # Same value shows up in the audit trail, not the client-supplied one.
    audit_logger.record.assert_called_once()
    detail = audit_logger.record.call_args[1]["detail_json"]
    assert detail["original_ai_value"] == 7.0


@pytest.mark.asyncio
async def test_override_unknown_parameter_raises_without_trusting_client_value():
    """If the parameter isn't in ai_findings at all, the override is rejected —
    the service never falls back to trusting the client's original_ai_value.
    """
    result = _make_result()
    db = _make_db_mock(result)
    audit_logger = MagicMock(spec=AuditLogger)
    request = MagicMock(spec=Request)

    service = ManualOverrideService(db=db, audit_logger=audit_logger)

    with pytest.raises(UnprocessableException) as exc_info:
        await service.override_parameter(
            result_id=RESULT_ID,
            parameter="nonexistent_parameter",
            corrected_value=1.0,
            rationale="test",
            original_ai_value=42.0,
            medtech_id=MEDTECH_ID,
            request=request,
        )
    assert exc_info.value.error_code == "PARAMETER_NOT_FOUND"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_override_rejected_after_result_finalised():
    """Cannot override a parameter once the result has been approved or
    returned for correction.
    """
    result = _make_result(status=ResultStatus.APPROVED)
    db = _make_db_mock(result)
    audit_logger = MagicMock(spec=AuditLogger)
    request = MagicMock(spec=Request)

    service = ManualOverrideService(db=db, audit_logger=audit_logger)

    with pytest.raises(UnprocessableException) as exc_info:
        await service.override_parameter(
            result_id=RESULT_ID,
            parameter="rbc_casts",
            corrected_value=1.0,
            rationale="test",
            original_ai_value=1.0,
            medtech_id=MEDTECH_ID,
            request=request,
        )
    assert exc_info.value.error_code == "RESULT_ALREADY_FINALISED"
    db.add.assert_not_called()
