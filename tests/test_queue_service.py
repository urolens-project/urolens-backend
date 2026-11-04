"""Unit tests — queue_service.py (UROLENS-142: race-safe specimen assignment,
unified workload definition).

`assignSpecimen`, `getWorkloads`, and `getReceptionistWorkloads` were
migrated off Supabase REST onto `AsyncSession` for this pass — this suite
mocks `sqlalchemyDb` accordingly (no real test database exists anywhere in
this repo). `getPendingSpecimens` is untouched and still Supabase-REST-based
(out of this ticket's scope), so it isn't covered here.

The *real* concurrency proof (two genuinely racing coroutines, not two
sequential calls) lives in
`tests/test_queue_assignment_concurrency.py` — see that file's module
docstring for why it needs a different, stateful fake session instead of the
call-ordered `AsyncMock`s used here.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import (
    NotFoundException,
    SpecimenNotFoundError,
    UnprocessableException,
)
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.queue import QueueAssignRequest
from src.services.notification_service import NotificationService
from src.services.queue_service import QueueService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000130")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000131")
ASSIGNED_BY = uuid.UUID("00000000-0000-0000-0000-000000000132")
ASSIGNMENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000133")


class _NestedCtx:
    """Fakes `AsyncSession.begin_nested()`'s async-context-manager protocol:
    let whatever exception the `async with` body raises propagate out
    unchanged, matching a real SAVEPOINT's behavior on an unhandled error.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, excType, exc, tb):
        return False


def _makeService(db):
    notificationService = MagicMock()
    notificationService.notify = AsyncMock()
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    service = QueueService(
        db=MagicMock(),
        auditLogger=auditLogger,
        _notificationService=notificationService,
        sqlalchemyDb=db,
    )
    return service, auditLogger, notificationService


def _makeAssignDb(
    specimenStatus: str = "LABELED",
    medtechExists: bool = True,
    medtechRole: str = "MEDTECH",
    medtechActive: bool = True,
    existingActiveAssignmentId: uuid.UUID | None = None,
    flushRaises: Exception | None = None,
    updateRowcount: int = 1,
):
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.status = specimenStatus
    specimen.sampleUid = "SMP-20260928-00001"

    medtech = None
    if medtechExists:
        medtech = MagicMock(spec=User)
        medtech.userId = MEDTECH_ID
        medtech.role = medtechRole
        medtech.isActive = medtechActive

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[specimen, medtech])

    existingResult = MagicMock()
    existingResult.scalar_one_or_none.return_value = existingActiveAssignmentId
    updateResult = MagicMock()
    updateResult.rowcount = updateRowcount
    db.execute = AsyncMock(side_effect=[existingResult, updateResult])

    async def _flush(objs):
        if flushRaises is not None:
            raise flushRaises
        for obj in objs:
            obj.assignmentId = ASSIGNMENT_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.begin_nested = MagicMock(return_value=_NestedCtx())
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db, specimen


def _makeRequest():
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    return request


class TestAssignSpecimen:
    @pytest.mark.asyncio
    async def test_assignSpecimenSuccess(self):
        db, specimen = _makeAssignDb()
        service, auditLogger, notificationService = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        response = await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert response.specimenId == SPECIMEN_ID
        assert response.medtechId == MEDTECH_ID
        assert response.assignmentId == ASSIGNMENT_ID
        assert response.status == "ACTIVE"
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()

        notificationService.notify.assert_awaited_once()
        assert notificationService.notify.call_args.args[1] == "New specimen assigned: SMP-20260928-00001"

        auditLogger.record.assert_awaited_once()
        args, kwargs = auditLogger.record.call_args
        assert args[0] == "QUEUE_ASSIGNED"
        assert kwargs["entityType"] == "queue_assignment"

    @pytest.mark.asyncio
    async def test_assignSpecimenUsesSampleUidNotRawUuidInNotification(self):
        """Item 4: the notification message must show sampleUid, not the raw
        specimen UUID.
        """
        db, specimen = _makeAssignDb()
        specimen.sampleUid = "SMP-20260928-99999"
        service, _, notificationService = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        message = notificationService.notify.call_args.args[1]
        assert "SMP-20260928-99999" in message
        assert str(SPECIMEN_ID) not in message

    @pytest.mark.asyncio
    async def test_assignSpecimenNotFound(self):
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        service, _, _ = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(SpecimenNotFoundError):
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

    @pytest.mark.asyncio
    async def test_assignSpecimenInvalidStatus(self):
        db, _ = _makeAssignDb(specimenStatus="RECEIVED")
        service, _, _ = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(UnprocessableException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "INVALID_SPECIMEN_STATUS"
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_assignSpecimenMedtechNotFound(self):
        db, _ = _makeAssignDb(medtechExists=False)
        service, _, _ = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(NotFoundException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "MEDTECH_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assignSpecimenMedtechInactiveRaisesMedtechNotFound(self):
        db, _ = _makeAssignDb(medtechActive=False)
        service, _, _ = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(NotFoundException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "MEDTECH_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assignSpecimenAlreadyAssignedViaPreCheck(self):
        db, _ = _makeAssignDb(existingActiveAssignmentId=uuid.uuid4())
        service, _, _ = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(UnprocessableException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "SPECIMEN_ALREADY_ASSIGNED"
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_assignSpecimenAlreadyAssignedViaConstraintViolation(self):
        """The race path: the pre-check passes (no ACTIVE row visible yet),
        but the insert itself violates the partial unique index — same
        SPECIMEN_ALREADY_ASSIGNED, not a raw IntegrityError/500.
        """
        integrityError = IntegrityError(
            "INSERT INTO queue_assignments ...",
            {},
            Exception(
                'duplicate key value violates unique constraint '
                '"ix_queue_assignments_one_active_per_specimen"'
            ),
        )
        db, _ = _makeAssignDb(flushRaises=integrityError)
        service, _, notificationService = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(UnprocessableException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "SPECIMEN_ALREADY_ASSIGNED"
        assert excInfo.value.status_code == 422
        notificationService.notify.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_assignSpecimenAlreadyAssignedViaConditionalUpdateRowcountZero(self):
        """The insert succeeds but the conditional UPDATE affects 0 rows
        (the specimen's status flipped between the first read and this
        UPDATE) — same SPECIMEN_ALREADY_ASSIGNED, and the assignment insert
        is rolled back rather than left orphaned.
        """
        db, _ = _makeAssignDb(updateRowcount=0)
        service, _, notificationService = _makeService(db)
        data = QueueAssignRequest(specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID)

        with pytest.raises(UnprocessableException) as excInfo:
            await service.assignSpecimen(data, ASSIGNED_BY, _makeRequest())

        assert excInfo.value.errorCode == "SPECIMEN_ALREADY_ASSIGNED"
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
        notificationService.notify.assert_not_awaited()


class TestWorkloads:
    def _makeCountsDb(self, rows: list[tuple[uuid.UUID, str, int]]):
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = rows
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_getWorkloadsReturnsSortedByQueueCount(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        db = self._makeCountsDb([(a, "medtech_a", 5), (b, "medtech_b", 0), (c, "medtech_c", 3)])
        service, _, _ = _makeService(db)

        workloads = await service.getWorkloads()

        assert [w.username for w in workloads] == ["medtech_b", "medtech_c", "medtech_a"]
        assert [w.queueCount for w in workloads] == [0, 3, 5]

    @pytest.mark.asyncio
    async def test_getWorkloadsEmptyWhenNoMedtechs(self):
        db = self._makeCountsDb([])
        service, _, _ = _makeService(db)

        assert await service.getWorkloads() == []

    @pytest.mark.asyncio
    async def test_getReceptionistWorkloadsUsernameAsFullNameDataGap(self):
        mid = uuid.uuid4()
        db = self._makeCountsDb([(mid, "medtech_a", 2)])
        service, _, _ = _makeService(db)

        items = await service.getReceptionistWorkloads()

        assert items[0].fullName == "medtech_a"

    @pytest.mark.asyncio
    async def test_workloadCountMatchesBetweenGetWorkloadsAndGetReceptionistWorkloads(self):
        """Item 3: same data, same number, from both call sites."""
        a, b = uuid.uuid4(), uuid.uuid4()
        rows = [(a, "medtech_a", 4), (b, "medtech_b", 1)]
        db = self._makeCountsDb(rows)
        service, _, _ = _makeService(db)

        workloads = await service.getWorkloads()
        # A fresh call re-executes the same query — reset side effects to
        # return the identical rows for the second call site.
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        receptionistItems = await service.getReceptionistWorkloads()

        workloadsByMedtech = {w.medtechId: w.queueCount for w in workloads}
        receptionistByMedtech = {i.userId: i.activeCount for i in receptionistItems}
        assert workloadsByMedtech == receptionistByMedtech == {a: 4, b: 1}


class TestNotificationService:
    @pytest.mark.asyncio
    async def test_notifyNeverRaises(self):
        db = AsyncMock()
        db.begin_nested = MagicMock()  # an async context manager (the savepoint)
        db.execute = AsyncMock(side_effect=Exception("DB error"))

        _service = NotificationService(db)
        userId = uuid.uuid4()

        await _service.notify(userId, "Test message", "TEST_TYPE")

        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notifyInsertsCorrectly(self):
        db = AsyncMock()
        db.begin_nested = MagicMock()  # an async context manager (the savepoint)
        # AsyncMock's attribute chaining makes nested auto-created attributes
        # AsyncMock too, so the result object must be pinned to a plain
        # MagicMock explicitly or `.scalar_one()` returns an unawaited
        # coroutine. A mock session has no commit hooks, so no push is queued.
        db.execute = AsyncMock(return_value=MagicMock())

        _service = NotificationService(db)
        userId = uuid.uuid4()
        entityId = uuid.uuid4()

        await _service.notify(userId, "Test", "TEST_TYPE", entityId=entityId)

        insertStmt = db.execute.call_args_list[0].args[0]
        params = insertStmt.compile().params
        assert params["user_id"] == userId
        assert params["message"] == "Test"
        assert params["notification_type"] == "TEST_TYPE"
        assert params["entity_id"] == entityId
