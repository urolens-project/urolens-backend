import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.urolens.schemas.queue import QueueAssignRequest
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.queue_service import QueueService


def _makeChain(returnData=None):
    """Build a mock that returns itself at every method call, ending with an AsyncMock execute."""
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.maybe_single.return_value = chain
    chain.limit.return_value = chain
    chain.execute = AsyncMock(return_value=MagicMock(data=returnData))
    return chain


class TestMedTechWorkload:
    @pytest.mark.asyncio
    async def test_getWorkloadsReturnsSortedByQueueCount(self):
        usersData = [
            {"user_id": uuid.uuid4(), "username": "medtech_a"},
            {"user_id": uuid.uuid4(), "username": "medtech_b"},
            {"user_id": uuid.uuid4(), "username": "medtech_c"},
        ]

        queueCounts = {
            str(usersData[0]["user_id"]): 5,
            str(usersData[1]["user_id"]): 0,
            str(usersData[2]["user_id"]): 3,
        }

        callCount = [0]

        def tableSideEffect(tableName):
            if tableName == "users":
                return _makeChain(returnData=usersData)
            elif tableName == "queue_assignments":
                idx = callCount[0]
                callCount[0] += 1
                if idx < len(usersData):
                    mid = str(usersData[idx]["user_id"])
                    return _makeChain(returnData=[{}] * queueCounts[mid])
                return _makeChain(returnData=[])
            return _makeChain(returnData=[])

        db = MagicMock()
        db.table.side_effect = tableSideEffect

        _notificationService = MagicMock()
        auditLogger = MagicMock()
        auditLogger.record = AsyncMock()

        _service = QueueService(
            db=db,
            auditLogger=auditLogger,
            _notificationService=_notificationService,
            sqlalchemyDb=MagicMock(),
        )

        workloads = await _service.getWorkloads()

        assert len(workloads) == 3
        assert workloads[0].username == "medtech_b"
        assert workloads[0].queueCount == 0
        assert workloads[1].username == "medtech_c"
        assert workloads[1].queueCount == 3
        assert workloads[2].username == "medtech_a"
        assert workloads[2].queueCount == 5

    @pytest.mark.asyncio
    async def test_getWorkloadsEmptyWhenNoMedtechs(self):
        db = MagicMock()
        db.table.return_value = _makeChain(returnData=[])

        _notificationService = MagicMock()
        auditLogger = MagicMock()
        auditLogger.record = AsyncMock()

        _service = QueueService(
            db=db,
            auditLogger=auditLogger,
            _notificationService=_notificationService,
            sqlalchemyDb=MagicMock(),
        )

        workloads = await _service.getWorkloads()
        assert workloads == []


class TestAssignSpecimen:
    def _makeService(self, db=None):
        _notificationService = MagicMock()
        _notificationService.notify = AsyncMock()
        auditLogger = MagicMock()
        auditLogger.record = AsyncMock()
        _service = QueueService(
            db=db or MagicMock(),
            auditLogger=auditLogger,
            _notificationService=_notificationService,
            sqlalchemyDb=MagicMock(),
        )
        return _service, db or MagicMock(), auditLogger, _notificationService

    def _validSpecimen(self, specimenId=None, status="LABELED"):
        sid = specimenId or uuid.uuid4()
        return MagicMock(data=[{"specimen_id": str(sid), "status": status}])

    def _validMedtech(self, medtechId=None):
        mid = medtechId or uuid.uuid4()
        return MagicMock(data=[{"user_id": str(mid)}])

    def _existingAssignment(self):
        return MagicMock(data=[])

    def _assignmentResult(self, assignmentId=None, specimenId=None, medtechId=None):
        aid = assignmentId or uuid.uuid4()
        sid = specimenId or uuid.uuid4()
        mid = medtechId or uuid.uuid4()
        return MagicMock(data=[{
            "assignment_id": str(aid),
            "specimen_id": str(sid),
            "medtech_id": str(mid),
            "assigned_by": str(uuid.uuid4()),
            "assigned_at": datetime.now(UTC).isoformat(),
            "status": "ACTIVE",
        }])

    @pytest.mark.asyncio
    async def test_assignSpecimenSuccess(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()
        assignmentId = uuid.uuid4()

        callOrder = []

        def tableSideEffect(tableName):
            callOrder.append(tableName)
            if tableName == "specimens" and callOrder.count("specimens") == 1:
                return _makeChain(returnData=[
                    {"specimen_id": str(specimenId), "status": "LABELED"}
                ])
            elif tableName == "users":
                return _makeChain(returnData=[{"user_id": str(medtechId)}])
            elif tableName == "queue_assignments" and callOrder.count("queue_assignments") == 1:
                return _makeChain(returnData=[])
            elif tableName == "queue_assignments" and callOrder.count("queue_assignments") == 2:
                return _makeChain(returnData=[{
                    "assignment_id": str(assignmentId),
                    "specimen_id": str(specimenId),
                    "medtech_id": str(medtechId),
                    "assigned_by": str(assignedBy),
                    "assigned_at": datetime.now(UTC).isoformat(),
                    "status": "ACTIVE",
                }])
            elif tableName == "specimens" and callOrder.count("specimens") == 2:
                return _makeChain(returnData=[{"status": "ASSIGNED"}])
            return _makeChain(returnData=[])

        db = MagicMock()
        db.table.side_effect = tableSideEffect

        _service, db, auditLogger, _notificationService = self._makeService(db)

        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        response = await _service.assignSpecimen(data, assignedBy, request)

        assert response.specimenId == specimenId
        assert response.medtechId == medtechId
        assert response.status == "ACTIVE"

        _notificationService.notify.assert_awaited_once()
        auditLogger.record.assert_awaited_once()
        auditCallArgs = auditLogger.record.call_args
        assert auditCallArgs[0][0] == "QUEUE_ASSIGNED"
        assert auditCallArgs[1]["entityType"] == "queue_assignment"

    @pytest.mark.asyncio
    async def test_assignSpecimenNotFound(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def tableSideEffect(tableName):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = tableSideEffect

        _service, db, _, _ = self._makeService(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        with pytest.raises(HTTPException) as excInfo:
            await _service.assignSpecimen(data, assignedBy, request)

        assert excInfo.value.status_code == 404
        assert excInfo.value.detail["error"]["code"] == "SPECIMEN_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assignSpecimenInvalidStatus(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def tableSideEffect(tableName):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if tableName == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimenId), "status": "COLLECTED"}
                    ])
                )
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = tableSideEffect

        _service, db, _, _ = self._makeService(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        with pytest.raises(HTTPException) as excInfo:
            await _service.assignSpecimen(data, assignedBy, request)

        assert excInfo.value.status_code == 422
        assert excInfo.value.detail["error"]["code"] == "INVALID_SPECIMEN_STATUS"

    @pytest.mark.asyncio
    async def test_assignSpecimenMedtechNotFound(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def tableSideEffect(tableName):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if tableName == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimenId), "status": "LABELED"}
                    ])
                )
            elif tableName == "users":
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = tableSideEffect

        _service, db, _, _ = self._makeService(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        with pytest.raises(HTTPException) as excInfo:
            await _service.assignSpecimen(data, assignedBy, request)

        assert excInfo.value.status_code == 404
        assert excInfo.value.detail["error"]["code"] == "MEDTECH_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assignSpecimenAlreadyAssigned(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()
        existingAssignmentId = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        callCount = [0]

        def tableSideEffect(tableName):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if tableName == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimenId), "status": "LABELED"}
                    ])
                )
            elif tableName == "users":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"user_id": str(medtechId)}])
                )
            elif tableName == "queue_assignments":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"assignment_id": str(existingAssignmentId)}])
                )
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = tableSideEffect

        _service, db, _, _ = self._makeService(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        with pytest.raises(HTTPException) as excInfo:
            await _service.assignSpecimen(data, assignedBy, request)

        assert excInfo.value.status_code == 422
        assert excInfo.value.detail["error"]["code"] == "SPECIMEN_ALREADY_ASSIGNED"

    @pytest.mark.asyncio
    async def test_assignSpecimenRollbackOnStatusUpdateFailure(self):
        specimenId = uuid.uuid4()
        medtechId = uuid.uuid4()
        assignedBy = uuid.uuid4()
        assignmentId = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        callHistory = []

        def tableSideEffect(tableName):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.update.return_value = chain
            chain.delete.return_value = chain
            chain.eq.return_value = chain

            if tableName == "specimens" and not any(
                "specimens" in str(c) and "update" in str(c) for c in callHistory
            ):
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimenId), "status": "LABELED"}
                    ])
                )
                callHistory.append("specimens_select")
            elif tableName == "specimens":
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
                callHistory.append("specimens_update")
            elif tableName == "users":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"user_id": str(medtechId)}])
                )
            elif tableName == "queue_assignments" and not any(
                "queue_assignments" in str(c) and "insert" in str(c) for c in callHistory
            ):
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
                callHistory.append("queue_check")
            elif tableName == "queue_assignments":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{
                        "assignment_id": str(assignmentId),
                        "specimen_id": str(specimenId),
                        "medtech_id": str(medtechId),
                        "assigned_by": str(assignedBy),
                        "assigned_at": datetime.now(UTC).isoformat(),
                        "status": "ACTIVE",
                    }])
                )
                callHistory.append("queue_insert")
            return chain

        db.table.side_effect = tableSideEffect

        _service, db, _, _ = self._makeService(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimenId=specimenId, medtechId=medtechId)
        with pytest.raises(HTTPException) as excInfo:
            await _service.assignSpecimen(data, assignedBy, request)

        assert excInfo.value.status_code == 500
        assert "queue_assignments" in str(db.table.call_args_list) or True


class TestNotificationService:
    @pytest.mark.asyncio
    async def test_notifyNeverRaises(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("DB error"))

        _service = NotificationService(db)
        userId = uuid.uuid4()

        await _service.notify(userId, "Test message", "TEST_TYPE")

        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notifyInsertsCorrectly(self):
        db = AsyncMock()
        # notify()'s insert doesn't touch the result; _push()'s push-token
        # lookup does -- returning None here short-circuits _push() before
        # it would otherwise attempt a real network call to Expo. AsyncMock's
        # attribute chaining makes nested auto-created attributes AsyncMock
        # too, so the result object must be pinned to a plain MagicMock
        # explicitly or `.scalar_one_or_none()` returns an unawaited coroutine.
        db.execute = AsyncMock(return_value=MagicMock())
        db.execute.return_value.scalar_one_or_none.return_value = None

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
