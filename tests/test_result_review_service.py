"""
Unit tests — ResultReviewService (supervisor review/approval, plan row 7)

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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.urolens.core.exceptions import ConflictException, UnprocessableException
from src.urolens.models.analysis_result import AnalysisResult, ResultStatus
from src.urolens.models.escalation import Escalation
from src.urolens.models.result_approval import ResultApproval
from src.urolens.models.result_return import ResultReturn
from src.urolens.models.result_review import ResultReview
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


# ── Regression test: spatial_annotations write-then-drop bug ──────────────


def _make_scalars_result(items: list) -> MagicMock:
    """A db.execute() return value shaped for `.scalars().all()`."""
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = items
    return execute_result


def _make_scalar_one_result(item) -> MagicMock:
    """A db.execute() return value shaped for `.scalar_one_or_none()`."""
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = item
    return execute_result


@pytest.mark.asyncio
async def test_annotate_result_persists_and_round_trips_spatial_annotations():
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
    ar_for_write = _make_result()
    write_db = AsyncMock()
    write_db.get = AsyncMock(return_value=ar_for_write)
    write_db.execute = AsyncMock(return_value=_make_scalar_one_result(None))
    write_db.add = MagicMock()
    write_db.commit = AsyncMock()

    write_service = ResultReviewService(db=write_db)
    write_response = await write_service.save_annotation(
        result_id=RESULT_ID,
        user_id=SUPERVISOR_ID,
        annotation_notes="Possible cast cluster, upper-left quadrant",
        spatial_annotations=payload,
    )

    write_db.add.assert_called_once()
    saved_review = write_db.add.call_args[0][0]
    assert isinstance(saved_review, ResultReview)
    # The actual regression: this attribute was never set on the pre-fix code.
    assert saved_review.spatial_annotations == payload
    assert write_response["spatial_annotations"] == payload

    # ── Read path: get_full_result must read the same saved object back ──
    saved_review.updated_at = datetime.now(timezone.utc)

    ar_for_read = _make_result()
    ar_for_read.image_id = None
    read_db = AsyncMock()
    read_db.get = AsyncMock(side_effect=[ar_for_read, None])  # AnalysisResult, then Specimen (none)
    read_db.execute = AsyncMock(
        side_effect=[
            _make_scalars_result([]),  # manual_overrides
            _make_scalar_one_result(saved_review),  # latest ResultReview
            _make_scalar_one_result(None),  # smart_diagnosis_output
        ]
    )

    read_service = ResultReviewService(db=read_db)
    detail = await read_service.get_full_result(RESULT_ID)

    assert detail["spatial_annotations"] == payload
    assert detail["annotation_notes"] == "Possible cast cluster, upper-left quadrant"


@pytest.mark.asyncio
async def test_annotate_result_omitting_spatial_annotations_preserves_existing_value():
    """Matches the pre-port behavior: calling save_annotation again without
    spatial_annotations (e.g. to update just the notes) must not clear a
    previously-saved value."""
    ar = _make_result()
    existing_review = MagicMock(spec=ResultReview)
    existing_review.spatial_annotations = [{"x": 1, "y": 2, "label": "prior"}]
    existing_review.annotation_notes = "old notes"

    db = AsyncMock()
    db.get = AsyncMock(return_value=ar)
    db.execute = AsyncMock(return_value=_make_scalar_one_result(existing_review))
    db.commit = AsyncMock()

    service = ResultReviewService(db=db)
    await service.save_annotation(
        result_id=RESULT_ID,
        user_id=SUPERVISOR_ID,
        annotation_notes="updated notes only",
        spatial_annotations=None,
    )

    assert existing_review.annotation_notes == "updated notes only"
    # Not overwritten with None just because this call didn't supply a value.
    assert existing_review.spatial_annotations == [{"x": 1, "y": 2, "label": "prior"}]
