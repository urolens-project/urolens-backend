"""Unit tests — ResultReviewService (supervisor review/approval, plan row 7)

Tier-1 workflow (standards skill's named example: confirm -> override ->
approve -> release). Baseline coverage per the original task's Task 5, not
exhaustive: one happy-path test per state-transition action
(approve/return/escalate), plus the guard shared by all three
(_require_pending rejects a result that isn't PENDING_SUPERVISOR_APPROVAL)
and the one input-validation branch escalate_result has that the others
don't.

save_annotation/get_full_result gained targeted coverage in a follow-up
fix for a real regression: spatial_annotations was accepted by
save_annotation but never actually persisted (the SQLAlchemy port never
assigned it onto the ResultReview object it saved), so every supervisor
annotation's spatial data was silently dropped. See
test_annotate_result_persists_and_round_trips_spatial_annotations below —
this is the test that would have caught it, and does: it fails against
the pre-fix code (asserting on the object passed to db.add, not just the
call succeeding) and passes against the fix. The rest of
ResultReviewService's read-side methods (get_pending, get_approved_today,
get_escalated, get_supervisor_stats) remain untested — still a gap, not
attempted here either, out of scope for the regression fix.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import ConflictException, UnprocessableException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.escalation import Escalation
from src.models.result_approval import ResultApproval
from src.models.result_return import ResultReturn
from src.models.result_review import ResultReview
from src.models.specimen import Specimen
from src.services.result_review_service import ResultReviewService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-000000000030")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000031")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000032")


def _makeResult(status: str = ResultStatus.PENDING_SUPERVISOR_APPROVAL) -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = status
    return result


def _makeSpecimen() -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = "ASSIGNED"
    specimen.completedAt = None
    return specimen


def _makeDbMock(getSideEffect: list) -> AsyncMock:
    """db.get(Model, id) returns the next item in get_side_effect, in call order."""
    db = AsyncMock()
    db.get = AsyncMock(side_effect=getSideEffect)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_approveResultTransitionsStatusAndCompletesSpecimen():
    """Happy path: approving a pending result records the approval, moves the
    result to APPROVED, and marks its specimen COMPLETED so it leaves the
    medtech's active queue.
    """
    result = _makeResult()
    specimen = _makeSpecimen()
    db = _makeDbMock(getSideEffect=[result, specimen])

    _service = ResultReviewService(db=db)
    response = await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes="Looks good")

    assert result.status == ResultStatus.APPROVED
    assert specimen.status == "COMPLETED"
    assert specimen.completedAt is not None
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.APPROVED.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultApproval)
    assert added.resultId == RESULT_ID
    assert added.approvedBy == SUPERVISOR_ID
    assert added.notes == "Looks good"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_returnResultTransitionsStatusAndRecordsReason():
    """Happy path: returning a pending result records the return reason and
    moves the result to RETURNED_FOR_CORRECTION.
    """
    result = _makeResult()
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    response = await _service.returnResult(RESULT_ID, SUPERVISOR_ID, reason="Blurry image")

    assert result.status == ResultStatus.RETURNED_FOR_CORRECTION
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.RETURNED_FOR_CORRECTION.value

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, ResultReturn)
    assert added.resultId == RESULT_ID
    assert added.returnedBy == SUPERVISOR_ID
    assert added.reason == "Blurry image"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalateResultTransitionsStatusAndRecordsPath():
    """Happy path: escalating a pending result records the escalation path
    and moves the result to CRITICAL_ESCALATED.
    """
    result = _makeResult()
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    response = await _service.escalateResult(
        RESULT_ID, SUPERVISOR_ID, escalationPath="MARK_CRITICAL", escalationNote="Urgent"
    )

    assert result.status == ResultStatus.CRITICAL_ESCALATED
    assert response["resultId"] == RESULT_ID
    assert response["status"] == ResultStatus.CRITICAL_ESCALATED.value
    assert response["escalationPath"] == "MARK_CRITICAL"

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, Escalation)
    assert added.resultId == RESULT_ID
    assert added.escalatedBy == SUPERVISOR_ID
    assert added.escalationPath == "MARK_CRITICAL"
    assert added.escalationNote == "Urgent"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_escalateResultRejectsInvalidEscalationPath():
    """An unrecognized escalation_path is rejected before any DB access —
    the same validation the pre-port Supabase service performed.
    """
    db = _makeDbMock(getSideEffect=[])

    _service = ResultReviewService(db=db)
    with pytest.raises(UnprocessableException) as excInfo:
        await _service.escalateResult(
            RESULT_ID, SUPERVISOR_ID, escalationPath="NOT_A_REAL_PATH", escalationNote=None
        )
    assert excInfo.value.errorCode == "INVALID_ESCALATION_PATH"
    db.get.assert_not_called()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_approveResultRejectsResultNotPending():
    """approve/return/escalate all share _require_pending — a result that
    isn't PENDING_SUPERVISOR_APPROVAL (e.g. already APPROVED) is rejected.
    """
    result = _makeResult(status=ResultStatus.APPROVED)
    db = _makeDbMock(getSideEffect=[result])

    _service = ResultReviewService(db=db)
    with pytest.raises(ConflictException) as excInfo:
        await _service.approveResult(RESULT_ID, SUPERVISOR_ID, notes=None)
    assert excInfo.value.errorCode == "INVALID_RESULT_STATUS"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ── Regression test: spatial_annotations write-then-drop bug ──────────────


def _makeScalarsResult(items: list) -> MagicMock:
    """A db.execute() return value shaped for `.scalars().all()`."""
    executeResult = MagicMock()
    executeResult.scalars.return_value.all.return_value = items
    return executeResult


def _makeScalarOneResult(item) -> MagicMock:
    """A db.execute() return value shaped for `.scalar_one_or_none()`."""
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = item
    return executeResult


@pytest.mark.asyncio
async def test_annotateResultPersistsAndRoundTripsSpatialAnnotations():
    """Regression test for the bug where save_annotation accepted
    spatial_annotations but never assigned it onto the ResultReview object
    it saved, so the value was silently dropped instead of persisted.

    This fails against the pre-fix code: asserting on the actual object
    passed to db.add (not just that the call succeeded) catches a missing
    assignment that a "did it raise?" test would miss entirely. It also
    checks the read side: get_full_result must surface the same value back,
    not just save_annotation's own echoed response.
    """
    payload = [
        {"x": 120, "y": 340, "label": "cast_cluster"},
        {"x": 55, "y": 90, "label": "crystal"},
    ]

    # ── Write path: no existing review row for this result/supervisor pair ──
    arForWrite = _makeResult()
    writeDb = AsyncMock()
    writeDb.get = AsyncMock(return_value=arForWrite)
    writeDb.execute = AsyncMock(return_value=_makeScalarOneResult(None))
    writeDb.add = MagicMock()
    writeDb.commit = AsyncMock()

    _writeService = ResultReviewService(db=writeDb)
    writeResponse = await _writeService.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        annotationNotes="Possible cast cluster, upper-left quadrant",
        spatialAnnotations=payload,
    )

    writeDb.add.assert_called_once()
    savedReview = writeDb.add.call_args[0][0]
    assert isinstance(savedReview, ResultReview)
    # The actual regression: this attribute was never set on the pre-fix code.
    assert savedReview.spatialAnnotations == payload
    assert writeResponse["spatialAnnotations"] == payload

    # ── Read path: get_full_result must read the same saved object back ──
    savedReview.updatedAt = datetime.now(UTC)

    arForRead = _makeResult()
    arForRead.imageId = None
    readDb = AsyncMock()
    readDb.get = AsyncMock(side_effect=[arForRead, None])  # AnalysisResult, then Specimen (none)
    readDb.execute = AsyncMock(
        side_effect=[
            _makeScalarsResult([]),  # manual_overrides
            _makeScalarOneResult(savedReview),  # latest ResultReview
            _makeScalarOneResult(None),  # smart_diagnosis_output
        ]
    )

    _readService = ResultReviewService(db=readDb)
    detail = await _readService.getFullResult(RESULT_ID)

    assert detail["spatialAnnotations"] == payload
    assert detail["annotationNotes"] == "Possible cast cluster, upper-left quadrant"


@pytest.mark.asyncio
async def test_annotateResultOmittingSpatialAnnotationsPreservesExistingValue():
    """Matches the pre-port behavior: calling save_annotation again without
    spatial_annotations (e.g. to update just the notes) must not clear a
    previously-saved value.
    """
    ar = _makeResult()
    existingReview = MagicMock(spec=ResultReview)
    existingReview.spatialAnnotations = [{"x": 1, "y": 2, "label": "prior"}]
    existingReview.annotationNotes = "old notes"

    db = AsyncMock()
    db.get = AsyncMock(return_value=ar)
    db.execute = AsyncMock(return_value=_makeScalarOneResult(existingReview))
    db.commit = AsyncMock()

    _service = ResultReviewService(db=db)
    await _service.saveAnnotation(
        resultId=RESULT_ID,
        userId=SUPERVISOR_ID,
        annotationNotes="updated notes only",
        spatialAnnotations=None,
    )

    assert existingReview.annotationNotes == "updated notes only"
    # Not overwritten with None just because this call didn't supply a value.
    assert existingReview.spatialAnnotations == [{"x": 1, "y": 2, "label": "prior"}]
