"""Unit tests — completed and historical samples (UROLENS-236).

Sync (`sync_service.pull`, read with SQLAlchemy):
- only the MedTech's unfinished samples and those finished in the last
  `HISTORY_WINDOW_DAYS` are sent;
- rows carry `completed_at`, `approved_at`, `released_at`, `particle_classes`;
- a delta lists what the phone should delete — samples that aged out since
  the last sync or are no longer assigned to the MedTech — per table;
- assignments delta on `assigned_at` (the table has no `updated_at`).

History (`medtech_history_service.listHistory`): one Reports category at a
time, the caller's own samples only, newest first with stable paging, patient
code only, audited. DB is mocked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from src.models.manual_override import ManualOverride
from src.models.queue_assignment import QueueAssignment
from src.models.result_approval import ResultApproval
from src.models.specimen import Specimen
from src.services import medtech_history_service, sync_service
from tests.conftest import (
    SYNC_MEDTECH_ID,
    makeSyncDb,
    syncQueries,
    syncResult,
    syncSpecimen,
)

SINCE = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)


def _sql(statement: Select) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Select) -> dict:
    return statement.compile(dialect=postgresql.dialect()).params


async def _pull(db: AsyncMock, since: datetime | None = None) -> tuple[dict, MagicMock]:
    auditLogger = MagicMock(record=AsyncMock())
    with patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        payload = await sync_service.pull(db, str(SYNC_MEDTECH_ID), since)
    return payload, auditLogger


# ── The sync window ───────────────────────────────────────────────────────────

def test_theSyncWindowIsThirtyDays() -> None:
    assert sync_service.HISTORY_WINDOW_DAYS == 30
    assert frozenset({"COMPLETED", "REJECTED"}) == sync_service.FINISHED_SPECIMEN_STATUSES


@pytest.mark.asyncio
async def test_syncSendsUnfinishedWorkAndSamplesFinishedWithinTheWindow() -> None:
    db = makeSyncDb()
    before = datetime.now(UTC)

    await _pull(db)

    [query] = syncQueries(db, Specimen, "Specimen")
    sql = _sql(query)
    assert "specimens.medtech_id = " in sql
    assert "specimens.status NOT IN" in sql
    assert (
        "coalesce(greatest(analysis_results.released_at, specimens.completed_at, specimens.rejected_at), "
        "specimens.updated_at) >= " in sql
    )
    params = _params(query)
    assert SYNC_MEDTECH_ID in params.values()
    [cutoff] = [v for v in params.values() if isinstance(v, datetime)]
    assert before - timedelta(days=30, seconds=5) <= cutoff <= datetime.now(UTC) - timedelta(days=30)


@pytest.mark.asyncio
async def test_aDeltaOnlySendsWhatChangedSinceTheLastSync() -> None:
    changed = syncSpecimen(updatedAt=SINCE + timedelta(minutes=1))
    untouched = syncSpecimen(updatedAt=SINCE - timedelta(days=1))
    changedResult = syncResult(untouched, status="APPROVED", updatedAt=SINCE + timedelta(minutes=2))
    db = makeSyncDb(rows=[(changed, None), (untouched, changedResult)])

    payload, _ = await _pull(db, SINCE)

    changes = payload["changes"]
    assert [s["id"] for s in changes["specimens"]["updated"]] == [str(changed.specimenId)]
    assert [r["id"] for r in changes["analysisResults"]["updated"]] == [str(changedResult.resultId)]
    assert changes["specimens"]["created"] == []
    [query] = syncQueries(db, Specimen, "Specimen")
    assert "specimens.updated_at > " in _sql(query) and "analysis_results.updated_at > " in _sql(query)


# ── History fields in the rows ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rowsCarryTheDatesAndFinalCountsHistoryNeeds() -> None:
    completedAt = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    releasedAt = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    approvedAt = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
    specimen = syncSpecimen(status="COMPLETED", completedAt=completedAt)
    result = syncResult(specimen, status="RELEASED", releasedAt=releasedAt, particleClasses={"erythrocytes": 5})
    db = makeSyncDb(rows=[(specimen, result)], approvals=[(result.resultId, approvedAt)])

    payload, _ = await _pull(db)

    [specimenRow] = payload["changes"]["specimens"]["created"]
    [resultRow] = payload["changes"]["analysisResults"]["created"]
    assert specimenRow["completed_at"] == completedAt.isoformat()
    assert resultRow["approved_at"] == approvedAt.isoformat()
    assert resultRow["released_at"] == releasedAt.isoformat()
    assert resultRow["particle_classes"] == {"erythrocytes": 5}
    [approvals] = syncQueries(db, ResultApproval, "resultId")
    assert "max(result_approvals.approved_at)" in _sql(approvals)


@pytest.mark.asyncio
async def test_aResultNeverApprovedHasNoApprovalDate() -> None:
    specimen = syncSpecimen()
    db = makeSyncDb(rows=[(specimen, syncResult(specimen))])

    payload, _ = await _pull(db)

    assert payload["changes"]["analysisResults"]["created"][0]["approved_at"] is None


# ── Removals ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aFullSyncRemovesNothingAndDoesNotLookForRemovals() -> None:
    db = makeSyncDb()

    payload, _ = await _pull(db)

    assert all(table["deleted"] == [] for table in payload["changes"].values())
    assert syncQueries(db, Specimen, "specimenId") == []


@pytest.mark.asyncio
async def test_aDeltaTellsThePhoneWhichSamplesToDropInEveryTable() -> None:
    goneSpecimen, goneResult, goneAssignment, goneOverride = (uuid.uuid4() for _ in range(4))
    rejectedSpecimen = uuid.uuid4()  # rejected before it had a result
    db = makeSyncDb(
        removed=[(goneSpecimen, goneResult), (rejectedSpecimen, None)],
        removedAssignmentIds=[goneAssignment],
        removedOverrideIds=[goneOverride],
    )

    payload, _ = await _pull(db, SINCE)

    changes = payload["changes"]
    assert sorted(changes["specimens"]["deleted"]) == sorted([str(goneSpecimen), str(rejectedSpecimen)])
    assert changes["analysisResults"]["deleted"] == [str(goneResult)]
    assert changes["queueAssignments"]["deleted"] == [str(goneAssignment)]
    assert changes["manualOverrides"]["deleted"] == [str(goneOverride)]
    [assignmentQuery] = syncQueries(db, QueueAssignment, "assignmentId")
    assert "queue_assignments.medtech_id = " in _sql(assignmentQuery)
    [overrideQuery] = syncQueries(db, ManualOverride, "overrideId")
    assert "manual_overrides.result_id IN" in _sql(overrideQuery)


@pytest.mark.asyncio
async def test_removalsCoverAgingOutAndReassignment() -> None:
    db = makeSyncDb()
    before = datetime.now(UTC)

    await _pull(db, SINCE)

    [query] = syncQueries(db, Specimen, "specimenId")
    sql = _sql(query)
    # Aged out: finished before today's cutoff, but inside the window at the last sync.
    assert "specimens.status IN" in sql
    assert "specimens.updated_at) < " in sql and "specimens.updated_at) >= " in sql
    # Reassigned: the MedTech had it, someone else (or nobody) has it now.
    assert "queue_assignments.medtech_id = " in sql
    assert "specimens.medtech_id IS DISTINCT FROM" in sql
    assert "specimens.updated_at > " in sql
    dates = sorted(v for v in _params(query).values() if isinstance(v, datetime))
    assert dates[0] == SINCE - timedelta(days=30)  # the window's edge at the last sync
    assert before - timedelta(days=30, seconds=5) <= dates[1] <= datetime.now(UTC) - timedelta(days=30)
    assert dates[2] == SINCE


@pytest.mark.asyncio
async def test_removalsAreAudited() -> None:
    gone = uuid.uuid4()
    changed = syncSpecimen(updatedAt=SINCE + timedelta(minutes=1))
    db = makeSyncDb(rows=[(changed, None)], removed=[(gone, None)])

    _, auditLogger = await _pull(db, SINCE)

    assert auditLogger.record.await_args.kwargs["detailJson"]["removed_specimen_ids"] == [str(gone)]


# ── Assignments ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assignmentsDeltaOnWhenTheyWereMadeAndStayInTheWindow() -> None:
    specimen = syncSpecimen()
    assignment = QueueAssignment(
        assignmentId=uuid.uuid4(), specimenId=specimen.specimenId, medtechId=SYNC_MEDTECH_ID,
        assignedAt=SINCE + timedelta(minutes=5), status="ACTIVE",
    )
    db = makeSyncDb(assignments=[assignment])

    payload, _ = await _pull(db, SINCE)

    [row] = payload["changes"]["queueAssignments"]["updated"]
    assert row == {
        "id": str(assignment.assignmentId), "specimen_id": str(specimen.specimenId),
        "medtech_id": str(SYNC_MEDTECH_ID), "assigned_at": assignment.assignedAt.isoformat(), "status": "ACTIVE",
    }
    [query] = syncQueries(db, QueueAssignment, "QueueAssignment")
    sql = _sql(query)
    assert "queue_assignments.assigned_at > " in sql
    assert "queue_assignments.specimen_id IN (SELECT specimens.specimen_id" in sql


# ── GET /results/medtech/history ──────────────────────────────────────────────

def _historyDb(total: int, rows: list[tuple]) -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one=MagicMock(return_value=total)),
        MagicMock(all=MagicMock(return_value=rows)),
    ])
    return db


async def _history(db: AsyncMock, category: str, page: int = 1, pageSize: int = 20) -> tuple[object, MagicMock]:
    auditLogger = MagicMock(record=AsyncMock())
    with patch.object(medtech_history_service, "AuditLogger", return_value=auditLogger):
        listing = await medtech_history_service.listHistory(db, SYNC_MEDTECH_ID, category, page, pageSize)
    return listing, auditLogger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "statusFilter", "reachedAt"),
    [
        ("PENDING_APPROVAL", "analysis_results.status = ", "analysis_results.confirmed_at"),
        ("APPROVED", "analysis_results.status = ", "max(result_approvals.approved_at)"),
        ("RELEASED", "analysis_results.status = ", "analysis_results.released_at"),
        ("REJECTED", "specimens.status = ", "specimens.rejected_at"),
    ],
)
async def test_historyListsTheCallersOwnSamplesInOneCategory(category: str, statusFilter: str, reachedAt: str) -> None:
    db = _historyDb(0, [])

    await _history(db, category)

    countQuery, rowsQuery = (c.args[0] for c in db.execute.await_args_list)
    sql = _sql(rowsQuery)
    assert "specimens.medtech_id = " in sql
    assert statusFilter in sql
    assert reachedAt in sql
    assert "ORDER BY \"finalizedAt\" DESC NULLS LAST, specimens.specimen_id" in sql
    assert "specimens.patient_name" not in sql
    assert SYNC_MEDTECH_ID in _params(rowsQuery).values()
    assert "count(*)" in _sql(countQuery)


@pytest.mark.asyncio
async def test_historyStatusFilterMatchesTheCategory() -> None:
    for category, status in (("PENDING_APPROVAL", "PENDING_SUPERVISOR_APPROVAL"), ("APPROVED", "APPROVED"),
                             ("RELEASED", "RELEASED"), ("REJECTED", "REJECTED")):
        db = _historyDb(0, [])
        await _history(db, category)
        rowsQuery = db.execute.await_args_list[1].args[0]
        assert status in [getattr(v, "value", v) for v in _params(rowsQuery).values()], category


@pytest.mark.asyncio
async def test_historyPagesWithOffsetAndLimit() -> None:
    db = _historyDb(45, [])

    listing, _ = await _history(db, "RELEASED", page=3, pageSize=10)

    rowsQuery = db.execute.await_args_list[1].args[0]
    params = _params(rowsQuery)
    assert (params["param_1"], params["param_2"]) == (10, 20)  # LIMIT, OFFSET
    assert (listing.total, listing.page, listing.pageSize) == (45, 3, 10)


@pytest.mark.asyncio
async def test_historyItemsIdentifyThePatientByCodeOnlyAndAreAudited() -> None:
    rejectedAt = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)  # well past the phone's window
    specimen = syncSpecimen(status="REJECTED", rejectionReason="INSUFFICIENT_VOLUME", rejectedAt=rejectedAt)
    db = _historyDb(1, [(specimen, None, rejectedAt)])

    listing, auditLogger = await _history(db, "REJECTED")

    [item] = listing.items
    assert item.specimenId == specimen.specimenId
    assert item.resultId is None
    assert (item.patientUid, item.sampleUid) == ("PAT-000001", "SMP-20260930-00001")
    assert item.category == "REJECTED"
    assert item.finalizedAt == rejectedAt
    assert item.rejectionReason == "INSUFFICIENT_VOLUME"
    assert "patientName" not in item.model_dump()
    kwargs = auditLogger.record.await_args.kwargs
    assert kwargs["eventType"] == "MEDTECH_HISTORY_VIEWED"
    assert kwargs["detailJson"] == {"category": "REJECTED", "specimen_ids": [str(specimen.specimenId)]}
    assert kwargs["db"] is db
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aRejectionReasonIsOnlyShownForRejectedSamples() -> None:
    specimen = syncSpecimen(status="COMPLETED", rejectionReason="STALE")
    db = _historyDb(1, [(specimen, uuid.uuid4(), datetime.now(UTC))])

    listing, _ = await _history(db, "APPROVED")

    assert listing.items[0].rejectionReason is None


@pytest.mark.asyncio
async def test_anEmptyHistoryPageIsNotAudited() -> None:
    db = _historyDb(0, [])

    listing, auditLogger = await _history(db, "APPROVED")

    assert listing.items == []
    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_resultCategoriesJoinTheirResultAndRejectedOuterJoinsIt() -> None:
    joined = _sql(medtech_history_service._categoryQuery(SYNC_MEDTECH_ID, "APPROVED"))
    rejected = _sql(medtech_history_service._categoryQuery(SYNC_MEDTECH_ID, "REJECTED"))
    assert "JOIN analysis_results" in joined and "LEFT OUTER JOIN analysis_results" not in joined
    assert "LEFT OUTER JOIN analysis_results" in rejected
