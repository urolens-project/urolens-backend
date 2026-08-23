"""Unit tests — NotificationService

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
async def test_notifyActiveReceptionistsNotifiesEachActiveReceptionist():
    receptionistIds = [uuid.uuid4(), uuid.uuid4()]
    db = AsyncMock()
    _service = NotificationService(db=db)
    _service._getActiveUserIds = AsyncMock(return_value=receptionistIds)
    _service.notify = AsyncMock()

    await _service.notifyActiveReceptionists(
        requestUid="REQ-20260822-00001",
        physicianName="dr_santos",
        labRequestId=LAB_REQUEST_ID,
    )

    _service._getActiveUserIds.assert_awaited_once_with(UserRole.RECEPTIONIST)
    assert _service.notify.await_count == 2
    for call in _service.notify.call_args_list:
        assert call.kwargs["notificationType"] == "LAB_REQUEST_SUBMITTED"
        assert call.kwargs["entityId"] == LAB_REQUEST_ID
        assert "REQ-20260822-00001" in call.kwargs["message"]
        assert "dr_santos" in call.kwargs["message"]


@pytest.mark.asyncio
async def test_notifyActiveReceptionistsNoRecipientsSendsNothing():
    db = AsyncMock()
    _service = NotificationService(db=db)
    _service._getActiveUserIds = AsyncMock(return_value=[])
    _service.notify = AsyncMock()

    await _service.notifyActiveReceptionists(
        requestUid="REQ-20260822-00002",
        physicianName="dr_santos",
        labRequestId=LAB_REQUEST_ID,
    )

    _service.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_getActiveUserIdsFiltersByRoleAndActiveStatus():
    db = AsyncMock()
    executeResult = MagicMock()
    ids = [uuid.uuid4()]
    executeResult.scalars.return_value.all.return_value = ids
    db.execute = AsyncMock(return_value=executeResult)

    _service = NotificationService(db=db)
    result = await _service._getActiveUserIds(UserRole.SUPERVISOR)

    assert result == ids
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_getActiveUserIdsReturnsEmptyListOnQueryFailure():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db unreachable"))

    _service = NotificationService(db=db)
    result = await _service._getActiveUserIds(UserRole.RECEPTIONIST)

    assert result == []
