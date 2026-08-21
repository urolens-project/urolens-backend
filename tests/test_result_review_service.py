"""
Unit tests — ResultReviewService (supervisor review/approval, plan row 7)

Tier-1 workflow (standards skill's named example: confirm -> override ->
approve -> release). Baseline coverage per this task's Task 5, not
exhaustive: one happy-path test per state-transition action
(approve/return/escalate), plus the guard shared by all three
(_require_pending rejects a result that isn't PENDING_SUPERVISOR_APPROVAL)
and the one input-validation branch escalate_result has that the others
don't. Read-side methods (get_pending, get_approved_today, get_escalated,
get_full_result, get_supervisor_stats) and save_annotation are NOT covered
here — flagged as a gap, not attempted, to keep this a baseline rather
than exhaustive coverage.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.urolens.core.exceptions import ConflictException, UnprocessableException
from src.urolens.models.analysis_result import AnalysisResult, ResultStatus
from src.urolens.models.escalation import Escalation
from src.urolens.models.result_approval import ResultApproval
from src.urolens.models.result_return import ResultReturn
from src.urolens.models.specimen import Specimen
from src.urolens.services.result_review_service import ResultReviewService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000031")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000032")


def _make_result(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.result_id = RESULT_ID
    result.specimen_id = SPECIMEN_ID
    result.status = status
    return result


def _make_specimen() -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimen_id = SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.completed_at = None
    return specimen


def _make_db_mock(get_side_effect: list) -> AsyncMock:
    """db.get(Model, id) returns the next item in get_side_effect, in call order."""
    db = AsyncMock()
    db.get = AsyncMock(side_effect=get_side_effect)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_approve_result_transitions_status_and_completes_specimen():
    """Happy path: approving a pending result records the approval, moves the
    result to APPROVED, and marks its specimen COMPLETED so it leaves the
    medtech's active queue."""
    result = _make_result()
    specimen = _make_specimen()
    db = _make_db_mock(get_side_effect=[result, specimen])

    service = ResultReviewService(db=db)
    response = await service.approve_result(RESULT_ID, SUPERVISOR_ID, notes="Looks good")

    assert result.status == ResultStatus.APPROVED
    assert specimen.status == "COMPLETED"
    assert specimen.completed_at is not None
    assert response["result_id"] == RESULT_ID
    assert response["status"] == ResultStatus.APPROVED.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultApproval)
    assert added.result_id == RESULT_ID
    assert added.approved_by == SUPERVISOR_ID
    assert added.notes == "Looks good"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_return_result_transitions_status_and_records_reason():
    """Happy path: returning a pending result records the return reason and
    moves the result to RETURNED_FOR_CORRECTION."""
    result = _make_result()
    db = _make_db_mock(get_side_effect=[result])

    service = ResultReviewService(db=db)
    response = await service.return_result(RESULT_ID, SUPERVISOR_ID, reason="Blurry image")

    assert result.status == ResultStatus.RETURNED_FOR_CORRECTION
    assert response["result_id"] == RESULT_ID
    assert response["status"] == ResultStatus.RETURNED_FOR_CORRECTION.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultReturn)
    assert added.result_id == RESULT_ID
    assert added.returned_by == SUPERVISOR_ID
    assert added.reason == "Blurry image"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalate_result_transitions_status_and_records_path():
    """Happy path: escalating a pending result records the escalation path
    and moves the result to CRITICAL_ESCALATED."""
    result = _make_result()
    db = _make_db_mock(get_side_effect=[result])

    service = ResultReviewService(db=db)
    response = await service.escalate_result(
        RESULT_ID, SUPERVISOR_ID, escalation_path="MARK_CRITICAL", escalation_note="Urgent"
    )

    assert result.status == ResultStatus.CRITICAL_ESCALATED
    assert response["result_id"] == RESULT_ID
    assert response["status"] == ResultStatus.CRITICAL_ESCALATED.value
    assert response["escalation_path"] == "MARK_CRITICAL"

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, Escalation)
    assert added.result_id == RESULT_ID
    assert added.escalated_by == SUPERVISOR_ID
    assert added.escalation_path == "MARK_CRITICAL"
    assert added.escalation_note == "Urgent"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalate_result_rejects_invalid_escalation_path():
    """An unrecognized escalation_path is rejected before any DB access —
    the same validation the pre-port Supabase service performed."""
    db = _make_db_mock(get_side_effect=[])

    service = ResultReviewService(db=db)
    with pytest.raises(UnprocessableException) as exc_info:
        await service.escalate_result(
            RESULT_ID, SUPERVISOR_ID, escalation_path="NOT_A_REAL_PATH", escalation_note=None
        )
    assert exc_info.value.error_code == "INVALID_ESCALATION_PATH"
    db.get.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_approve_result_rejects_result_not_pending():
    """approve/return/escalate all share _require_pending — a result that
    isn't PENDING_SUPERVISOR_APPROVAL (e.g. already APPROVED) is rejected."""
    result = _make_result(status=ResultStatus.APPROVED)
    db = _make_db_mock(get_side_effect=[result])

    service = ResultReviewService(db=db)
    with pytest.raises(ConflictException) as exc_info:
        await service.approve_result(RESULT_ID, SUPERVISOR_ID, notes=None)
    assert exc_info.value.error_code == "INVALID_RESULT_STATUS"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
