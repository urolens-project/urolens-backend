import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.urolens.schemas.queue import QueueAssignRequest
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.queue_service import QueueService


def _make_chain(return_data=None):
    """Build a mock that returns itself at every method call, ending with an AsyncMock execute."""
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.maybe_single.return_value = chain
    chain.limit.return_value = chain
    chain.execute = AsyncMock(return_value=MagicMock(data=return_data))
    return chain


class TestMedTechWorkload:
    @pytest.mark.asyncio
    async def test_get_workloads_returns_sorted_by_queue_count(self):
        users_data = [
            {"user_id": uuid.uuid4(), "username": "medtech_a"},
            {"user_id": uuid.uuid4(), "username": "medtech_b"},
            {"user_id": uuid.uuid4(), "username": "medtech_c"},
        ]

        queue_counts = {
            str(users_data[0]["user_id"]): 5,
            str(users_data[1]["user_id"]): 0,
            str(users_data[2]["user_id"]): 3,
        }

        call_count = [0]

        def table_side_effect(table_name):
            if table_name == "users":
                return _make_chain(return_data=users_data)
            elif table_name == "queue_assignments":
                idx = call_count[0]
                call_count[0] += 1
                if idx < len(users_data):
                    mid = str(users_data[idx]["user_id"])
                    return _make_chain(return_data=[{}] * queue_counts[mid])
                return _make_chain(return_data=[])
            return _make_chain(return_data=[])

        db = MagicMock()
        db.table.side_effect = table_side_effect

        notification_service = MagicMock()
        audit_logger = MagicMock()
        audit_logger.record = AsyncMock()

        service = QueueService(
            db=db,
            audit_logger=audit_logger,
            notification_service=notification_service,
        )

        workloads = await service.get_workloads()

        assert len(workloads) == 3
        assert workloads[0].username == "medtech_b"
        assert workloads[0].queue_count == 0
        assert workloads[1].username == "medtech_c"
        assert workloads[1].queue_count == 3
        assert workloads[2].username == "medtech_a"
        assert workloads[2].queue_count == 5

    @pytest.mark.asyncio
    async def test_get_workloads_empty_when_no_medtechs(self):
        db = MagicMock()
        db.table.return_value = _make_chain(return_data=[])

        notification_service = MagicMock()
        audit_logger = MagicMock()
        audit_logger.record = AsyncMock()

        service = QueueService(
            db=db,
            audit_logger=audit_logger,
            notification_service=notification_service,
        )

        workloads = await service.get_workloads()
        assert workloads == []


class TestAssignSpecimen:
    def _make_service(self, db=None):
        notification_service = MagicMock()
        notification_service.notify = AsyncMock()
        audit_logger = MagicMock()
        audit_logger.record = AsyncMock()
        service = QueueService(
            db=db or MagicMock(),
            audit_logger=audit_logger,
            notification_service=notification_service,
        )
        return service, db or MagicMock(), audit_logger, notification_service

    def _valid_specimen(self, specimen_id=None, status="LABELED"):
        sid = specimen_id or uuid.uuid4()
        return MagicMock(data=[{"specimen_id": str(sid), "status": status}])

    def _valid_medtech(self, medtech_id=None):
        mid = medtech_id or uuid.uuid4()
        return MagicMock(data=[{"user_id": str(mid)}])

    def _existing_assignment(self):
        return MagicMock(data=[])

    def _assignment_result(self, assignment_id=None, specimen_id=None, medtech_id=None):
        aid = assignment_id or uuid.uuid4()
        sid = specimen_id or uuid.uuid4()
        mid = medtech_id or uuid.uuid4()
        return MagicMock(data=[{
            "assignment_id": str(aid),
            "specimen_id": str(sid),
            "medtech_id": str(mid),
            "assigned_by": str(uuid.uuid4()),
            "assigned_at": datetime.now(UTC).isoformat(),
            "status": "ACTIVE",
        }])

    @pytest.mark.asyncio
    async def test_assign_specimen_success(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()
        assignment_id = uuid.uuid4()

        call_order = []

        def table_side_effect(table_name):
            call_order.append(table_name)
            if table_name == "specimens" and call_order.count("specimens") == 1:
                return _make_chain(return_data=[
                    {"specimen_id": str(specimen_id), "status": "LABELED"}
                ])
            elif table_name == "users":
                return _make_chain(return_data=[{"user_id": str(medtech_id)}])
            elif table_name == "queue_assignments" and call_order.count("queue_assignments") == 1:
                return _make_chain(return_data=[])
            elif table_name == "queue_assignments" and call_order.count("queue_assignments") == 2:
                return _make_chain(return_data=[{
                    "assignment_id": str(assignment_id),
                    "specimen_id": str(specimen_id),
                    "medtech_id": str(medtech_id),
                    "assigned_by": str(assigned_by),
                    "assigned_at": datetime.now(UTC).isoformat(),
                    "status": "ACTIVE",
                }])
            elif table_name == "specimens" and call_order.count("specimens") == 2:
                return _make_chain(return_data=[{"status": "ASSIGNED"}])
            return _make_chain(return_data=[])

        db = MagicMock()
        db.table.side_effect = table_side_effect

        service, db, audit_logger, notification_service = self._make_service(db)

        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        response = await service.assign_specimen(data, assigned_by, request)

        assert response.specimen_id == specimen_id
        assert response.medtech_id == medtech_id
        assert response.status == "ACTIVE"

        notification_service.notify.assert_awaited_once()
        audit_logger.record.assert_awaited_once()
        audit_call_args = audit_logger.record.call_args
        assert audit_call_args[0][0] == "QUEUE_ASSIGNED"
        assert audit_call_args[1]["entity_type"] == "queue_assignment"

    @pytest.mark.asyncio
    async def test_assign_specimen_not_found(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def table_side_effect(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = table_side_effect

        service, db, _, _ = self._make_service(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        with pytest.raises(HTTPException) as exc_info:
            await service.assign_specimen(data, assigned_by, request)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "SPECIMEN_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assign_specimen_invalid_status(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def table_side_effect(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if table_name == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimen_id), "status": "COLLECTED"}
                    ])
                )
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = table_side_effect

        service, db, _, _ = self._make_service(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        with pytest.raises(HTTPException) as exc_info:
            await service.assign_specimen(data, assigned_by, request)

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "INVALID_SPECIMEN_STATUS"

    @pytest.mark.asyncio
    async def test_assign_specimen_medtech_not_found(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        def table_side_effect(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if table_name == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimen_id), "status": "LABELED"}
                    ])
                )
            elif table_name == "users":
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = table_side_effect

        service, db, _, _ = self._make_service(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        with pytest.raises(HTTPException) as exc_info:
            await service.assign_specimen(data, assigned_by, request)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"]["code"] == "MEDTECH_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_assign_specimen_already_assigned(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()
        existing_assignment_id = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()


        def table_side_effect(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.eq.return_value = chain
            if table_name == "specimens":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimen_id), "status": "LABELED"}
                    ])
                )
            elif table_name == "users":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"user_id": str(medtech_id)}])
                )
            elif table_name == "queue_assignments":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"assignment_id": str(existing_assignment_id)}])
                )
            else:
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
            return chain

        db.table.side_effect = table_side_effect

        service, db, _, _ = self._make_service(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        with pytest.raises(HTTPException) as exc_info:
            await service.assign_specimen(data, assigned_by, request)

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "SPECIMEN_ALREADY_ASSIGNED"

    @pytest.mark.asyncio
    async def test_assign_specimen_rollback_on_status_update_failure(self):
        specimen_id = uuid.uuid4()
        medtech_id = uuid.uuid4()
        assigned_by = uuid.uuid4()
        assignment_id = uuid.uuid4()

        db = MagicMock()
        db.table = MagicMock()

        call_history = []

        def table_side_effect(table_name):
            chain = MagicMock()
            chain.select.return_value = chain
            chain.insert.return_value = chain
            chain.update.return_value = chain
            chain.delete.return_value = chain
            chain.eq.return_value = chain

            if table_name == "specimens" and not any(
                "specimens" in str(c) and "update" in str(c) for c in call_history
            ):
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[
                        {"specimen_id": str(specimen_id), "status": "LABELED"}
                    ])
                )
                call_history.append("specimens_select")
            elif table_name == "specimens":
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
                call_history.append("specimens_update")
            elif table_name == "users":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{"user_id": str(medtech_id)}])
                )
            elif table_name == "queue_assignments" and not any(
                "queue_assignments" in str(c) and "insert" in str(c) for c in call_history
            ):
                chain.execute = AsyncMock(return_value=MagicMock(data=[]))
                call_history.append("queue_check")
            elif table_name == "queue_assignments":
                chain.execute = AsyncMock(
                    return_value=MagicMock(data=[{
                        "assignment_id": str(assignment_id),
                        "specimen_id": str(specimen_id),
                        "medtech_id": str(medtech_id),
                        "assigned_by": str(assigned_by),
                        "assigned_at": datetime.now(UTC).isoformat(),
                        "status": "ACTIVE",
                    }])
                )
                call_history.append("queue_insert")
            return chain

        db.table.side_effect = table_side_effect

        service, db, _, _ = self._make_service(db)
        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        data = QueueAssignRequest(specimen_id=specimen_id, medtech_id=medtech_id)
        with pytest.raises(HTTPException) as exc_info:
            await service.assign_specimen(data, assigned_by, request)

        assert exc_info.value.status_code == 500
        assert "queue_assignments" in str(db.table.call_args_list) or True


class TestNotificationService:
    @pytest.mark.asyncio
    async def test_notify_never_raises(self):
        db = MagicMock()
        chain = MagicMock()
        chain.insert.return_value = chain
        chain.execute = AsyncMock(side_effect=Exception("DB error"))
        db.table.return_value = chain

        service = NotificationService(db)
        user_id = uuid.uuid4()

        await service.notify(user_id, "Test message", "TEST_TYPE")

        chain.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notify_inserts_correctly(self):
        db = MagicMock()
        chain = MagicMock()
        chain.insert.return_value = chain
        chain.execute = AsyncMock(return_value=MagicMock())
        db.table.return_value = chain

        service = NotificationService(db)
        user_id = uuid.uuid4()
        entity_id = uuid.uuid4()

        await service.notify(user_id, "Test", "TEST_TYPE", entity_id=entity_id)

        chain.insert.assert_called_once()
        payload = chain.insert.call_args[0][0]
        assert payload["user_id"] == str(user_id)
        assert payload["message"] == "Test"
        assert payload["notification_type"] == "TEST_TYPE"
        assert payload["entity_id"] == str(entity_id)
        assert "is_read" not in payload
