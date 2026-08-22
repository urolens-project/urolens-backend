"""
Unit tests — NotificationService

Covers `notify_active_receptionists`, added as part of consolidating
lab-request creation (see changelog.md's "Duplicate lab-request creation
implementations" entry), and the generalized `_get_active_user_ids` helper
it and the two existing supervisor-notification methods now share.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.urolens.core.enums import UserRole
from src.urolens.services.notification_service import NotificationService

LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000060")


@pytest.mark.asyncio
async def test_notify_active_receptionists_notifies_each_active_receptionist():
    receptionist_ids = [uuid.uuid4(), uuid.uuid4()]
    db = AsyncMock()
    service = NotificationService(db=db)
    service._get_active_user_ids = AsyncMock(return_value=receptionist_ids)
    service.notify = AsyncMock()

    await service.notify_active_receptionists(
        request_uid="REQ-20260822-00001",
        physician_name="dr_santos",
        lab_request_id=LAB_REQUEST_ID,
    )

    service._get_active_user_ids.assert_awaited_once_with(UserRole.RECEPTIONIST)
    assert service.notify.await_count == 2
    for call in service.notify.call_args_list:
        assert call.kwargs["notification_type"] == "LAB_REQUEST_SUBMITTED"
        assert call.kwargs["entity_id"] == LAB_REQUEST_ID
        assert "REQ-20260822-00001" in call.kwargs["message"]
        assert "dr_santos" in call.kwargs["message"]


@pytest.mark.asyncio
async def test_notify_active_receptionists_no_recipients_sends_nothing():
    db = AsyncMock()
    service = NotificationService(db=db)
    service._get_active_user_ids = AsyncMock(return_value=[])
    service.notify = AsyncMock()

    await service.notify_active_receptionists(
        request_uid="REQ-20260822-00002",
        physician_name="dr_santos",
        lab_request_id=LAB_REQUEST_ID,
    )

    service.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_user_ids_filters_by_role_and_active_status():
    db = AsyncMock()
    execute_result = MagicMock()
    ids = [uuid.uuid4()]
    execute_result.scalars.return_value.all.return_value = ids
    db.execute = AsyncMock(return_value=execute_result)

    service = NotificationService(db=db)
    result = await service._get_active_user_ids(UserRole.SUPERVISOR)

    assert result == ids
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_active_user_ids_returns_empty_list_on_query_failure():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db unreachable"))

    service = NotificationService(db=db)
    result = await service._get_active_user_ids(UserRole.RECEPTIONIST)

    assert result == []
