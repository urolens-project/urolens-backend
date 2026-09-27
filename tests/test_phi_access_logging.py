"""Unit tests — logging who viewed patient data on mobile routes (UROLENS-222, RA 10173).

Each view that returns patient data is recorded in the request's own
transaction (`db=` + commit), so a view can't happen without its audit row:
- `GET /results/{id}` -> `RESULT_DETAIL_VIEWED`
- `GET /results/medtech/pending` -> `PENDING_RESULTS_VIEWED` (result IDs shown)
- `GET /sync/pull` -> `SYNC_PULLED` (specimen IDs sent)
Views that return nothing aren't logged. DB and Supabase are mocked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.analysis_result import ResultStatus
from src.services import sync_service
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_review_service import ResultReviewService
from tests.test_result_review_service import (
    RESULT_ID,
    _makeResult,
    _makeScalarOneResult,
    _makeScalarsResult,
    _makeSpecimen,
)

VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


def _makeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


# ── Result detail ─────────────────────────────────────────────────────────────

def _makeDetailDb() -> AsyncMock:
    ar = _makeResult(status=ResultStatus.PENDING_SUPERVISOR_APPROVAL)
    ar.imageId = None
    ar.confirmedAt = None
    ar.aiFindings = {}
    ar.flaggedAnomalies = {}
    ar.particleClasses = {}
    ar.modelVersion = "mvp-v1.0"
    ar.smartDiagnosisUnavailable = False
    specimen = _makeSpecimen()
    specimen.patientUid = None
    specimen.medtechId = None
    specimen.patientName = None
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[ar, specimen])
    db.execute = AsyncMock(side_effect=[
        _makeScalarsResult([]), _makeScalarOneResult(None), _makeScalarOneResult(None),
    ])
    return db


@pytest.mark.asyncio
async def test_resultDetailViewIsRecordedInTheRequestsTransaction():
    db = _makeDetailDb()
    auditLogger = _makeAuditLogger()

    detail = await ResultReviewService(db=db, auditLogger=auditLogger).getFullResult(
        RESULT_ID, viewerId=VIEWER_ID, request="req"
    )

    assert detail["resultId"] == RESULT_ID
    kwargs = auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "RESULT_DETAIL_VIEWED"
    assert kwargs["entityId"] == RESULT_ID
    assert kwargs["userId"] == VIEWER_ID
    assert kwargs["db"] is db
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resultDetailWithoutAViewerIsNotLogged():
    # Internal callers (no viewer) keep the old read-only behaviour.
    db = _makeDetailDb()
    auditLogger = _makeAuditLogger()

    await ResultReviewService(db=db, auditLogger=auditLogger).getFullResult(RESULT_ID)

    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── MedTech pending list ──────────────────────────────────────────────────────

def _makeConfirmationService(db: AsyncMock, auditLogger: MagicMock) -> ResultConfirmationService:
    return ResultConfirmationService(
        db=db, auditLogger=auditLogger, _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )


@pytest.mark.asyncio
async def test_pendingListViewRecordsWhichResultsWereShown():
    db = AsyncMock()
    auditLogger = _makeAuditLogger()
    service = _makeConfirmationService(db, auditLogger)
    shown = [uuid.uuid4(), uuid.uuid4()]
    listing = {"items": [{"resultId": r} for r in shown], "total": 2, "page": 1, "pageSize": 20}

    with patch.object(service, "_queryPendingForMedtech", AsyncMock(return_value=listing)):
        out = await service.listPendingForMedtech(VIEWER_ID, 1, 20, request="req")

    assert out is listing
    kwargs = auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "PENDING_RESULTS_VIEWED"
    assert kwargs["detailJson"] == {"result_ids": [str(r) for r in shown]}
    assert kwargs["db"] is db
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_emptyPendingListIsNotLogged():
    db = AsyncMock()
    auditLogger = _makeAuditLogger()
    service = _makeConfirmationService(db, auditLogger)
    empty = {"items": [], "total": 0, "page": 1, "pageSize": 20}

    with patch.object(service, "_queryPendingForMedtech", AsyncMock(return_value=empty)):
        await service.listPendingForMedtech(VIEWER_ID, 1, 20)

    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


# ── Sync pull ─────────────────────────────────────────────────────────────────

def _makeSyncSupabase(specimenRows: list[dict], resultRows: list[dict]) -> MagicMock:
    # Every chain method returns the same query object; execute() answers per table.
    def _table(name: str) -> MagicMock:
        query = MagicMock()
        for method in ("select", "eq", "gt", "in_"):
            getattr(query, method).return_value = query
        data = {"specimens": specimenRows, "analysis_results": resultRows}.get(name, [])
        query.execute = AsyncMock(return_value=MagicMock(data=data))
        return query

    sb = MagicMock()
    sb.table.side_effect = _table
    return sb


@pytest.mark.asyncio
async def test_syncPullRecordsWhichSpecimensWereSentToTheDevice():
    db = AsyncMock()
    auditLogger = _makeAuditLogger()
    specimenId = str(uuid.uuid4())
    resultId = str(uuid.uuid4())
    sb = _makeSyncSupabase(
        specimenRows=[{"specimen_id": specimenId, "patient_name": "ciphertext"}],
        resultRows=[{"result_id": resultId, "specimen_id": specimenId}],
    )

    with patch.object(sync_service, "supabase", sb), \
         patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        payload = await sync_service.pull(db, str(VIEWER_ID), None, request="req")

    assert len(payload["changes"]["specimens"]["created"]) == 1
    kwargs = auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "SYNC_PULLED"
    assert kwargs["detailJson"] == {"delta": False, "specimen_ids": [specimenId], "result_ids": [resultId]}
    assert kwargs["db"] is db
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_emptySyncPullIsNotLogged():
    db = AsyncMock()
    auditLogger = _makeAuditLogger()

    with patch.object(sync_service, "supabase", _makeSyncSupabase([], [])), \
         patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        await sync_service.pull(db, str(VIEWER_ID), None)

    auditLogger.record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_deltaPullRecordsResultsEvenWhenTheirSpecimensDidNotChange():
    # A supervisor returning a result changes the result, not the specimen:
    # the audit row must still say whose result reached the device.
    db = AsyncMock()
    auditLogger = _makeAuditLogger()
    since = datetime.now(UTC) - timedelta(minutes=5)
    oldSpecimen = {"specimen_id": str(uuid.uuid4()), "updated_at": (since - timedelta(days=1)).isoformat()}
    resultId = str(uuid.uuid4())
    sb = _makeSyncSupabase([oldSpecimen], [{"result_id": resultId, "specimen_id": oldSpecimen["specimen_id"]}])

    with patch.object(sync_service, "supabase", sb), \
         patch.object(sync_service, "AuditLogger", return_value=auditLogger):
        await sync_service.pull(db, str(VIEWER_ID), since)

    detail = auditLogger.record.call_args.kwargs["detailJson"]
    assert detail["specimen_ids"] == []
    assert detail["result_ids"] == [resultId]


@pytest.mark.asyncio
async def test_syncTimestampIsTakenBeforeTheReadsSoConcurrentUpdatesAreNotSkipped():
    db = AsyncMock()
    readStarted: list[str] = []
    sb = _makeSyncSupabase([], [])
    original = sb.table.side_effect

    def _recordingTable(name: str):
        readStarted.append(datetime.now(UTC).isoformat())
        return original(name)

    sb.table.side_effect = _recordingTable
    with patch.object(sync_service, "supabase", sb):
        payload = await sync_service.pull(db, str(VIEWER_ID), None)

    assert payload["timestamp"] <= min(readStarted)
