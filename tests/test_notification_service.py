"""Unit tests — NotificationService

Covers `notifyActiveReceptionists`, added as part of consolidating
lab-request creation (see changelog.md's "Duplicate lab-request creation
implementations" entry), and the generalized `_getActiveUserIds` helper
it and the two existing supervisor-notification methods now share.

Push delivery (UROLENS-248), on a real `AsyncSession` with its statements
mocked: nothing is sent before the commit; the commit sends everything queued
in one request, with the notification ID; a rollback sends nothing; users
without an Expo token get none; a failed insert or a failed send never raises;
the returned-result message names the sample only.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from src.core.enums import UserRole
from src.services import notification_service
from src.services.notification_service import NotificationService

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


# ── Push delivery after commit (UROLENS-248) ──────────────────────────────────

NOTIFICATION_IDS = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{n:02d}") for n in range(70, 80)]
EXPO_TOKEN = "ExponentPushToken[abc123]"


class _FakeExpo:
    """Stands in for `httpx.AsyncClient`, recording what would be posted to Expo."""

    def __init__(self, fail: bool = False) -> None:
        self.posts: list[list[dict]] = []
        self.fail = fail

    def __call__(self, **_kwargs: object) -> _FakeExpo:
        return self

    async def __aenter__(self) -> _FakeExpo:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, json: list[dict]) -> MagicMock:
        assert url == notification_service.EXPO_PUSH_URL
        if self.fail:
            raise RuntimeError("Expo unreachable")
        self.posts.append(json)
        return MagicMock(status_code=200)


def _session(token: str | None = EXPO_TOKEN, failInsert: bool = False) -> AsyncSession:
    # A real session (so its commit/rollback hooks fire) with the database
    # replaced: inserts return the next notification ID, the token lookup
    # returns `token`. A transaction is begun as the first real query would.
    session = AsyncSession()
    ids = iter(NOTIFICATION_IDS)

    async def _execute(stmt: object) -> MagicMock:
        result = MagicMock()
        if isinstance(stmt, Insert):
            if failInsert:
                raise RuntimeError("insert failed")
            result.scalar_one.return_value = next(ids)
        else:
            result.scalar_one_or_none.return_value = token
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.begin_nested = MagicMock()  # the savepoint, as an async context manager
    session.sync_session.begin()
    return session


async def _sent(expo: _FakeExpo) -> list[list[dict]]:
    await asyncio.gather(*notification_service._pushTasks)
    return expo.posts


@pytest.mark.asyncio
async def test_aPushGoesOutOnlyAfterTheCommit() -> None:
    session, expo = _session(), _FakeExpo()
    entityId = uuid.uuid4()

    with patch.object(notification_service.httpx, "AsyncClient", expo):
        await NotificationService(session).notify(uuid.uuid4(), "New specimen assigned", "SAMPLE_ASSIGNED", entityId)
        await asyncio.sleep(0)
        assert expo.posts == []

        await session.commit()
        posts = await _sent(expo)

    assert posts == [[{
        "to": EXPO_TOKEN,
        "title": "UroLens",
        "body": "New specimen assigned",
        "data": {
            "notification_id": str(NOTIFICATION_IDS[0]),
            "notification_type": "SAMPLE_ASSIGNED",
            "entity_id": str(entityId),
        },
        "sound": "default",
    }]]
    session.begin_nested.assert_called_once()


@pytest.mark.asyncio
async def test_everythingQueuedInOneTransactionGoesInOneRequest() -> None:
    session, expo = _session(), _FakeExpo()

    with patch.object(notification_service.httpx, "AsyncClient", expo):
        service = NotificationService(session)
        for _ in range(3):
            await service.notify(uuid.uuid4(), "Ready for review", "RESULT_READY_FOR_REVIEW")
        await session.commit()
        posts = await _sent(expo)

    assert len(posts) == 1
    assert [m["data"]["notification_id"] for m in posts[0]] == [str(i) for i in NOTIFICATION_IDS[:3]]


@pytest.mark.asyncio
async def test_aRolledBackTransactionSendsNothingEvenAfterALaterCommit() -> None:
    session, expo = _session(), _FakeExpo()

    with patch.object(notification_service.httpx, "AsyncClient", expo):
        await NotificationService(session).notify(uuid.uuid4(), "New specimen assigned", "SAMPLE_ASSIGNED")
        await session.rollback()
        session.sync_session.begin()
        await session.commit()
        posts = await _sent(expo)

    assert posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "fcm:abc123"])
async def test_aUserWithoutAnExpoTokenGetsNoPush(token: str | None) -> None:
    session, expo = _session(token=token), _FakeExpo()

    with patch.object(notification_service.httpx, "AsyncClient", expo):
        await NotificationService(session).notify(uuid.uuid4(), "New specimen assigned", "SAMPLE_ASSIGNED")
        await session.commit()
        posts = await _sent(expo)

    assert posts == []


@pytest.mark.asyncio
async def test_aFailedInsertNeverRaisesAndQueuesNothing() -> None:
    session, expo = _session(failInsert=True), _FakeExpo()

    with patch.object(notification_service.httpx, "AsyncClient", expo):
        await NotificationService(session).notify(uuid.uuid4(), "New specimen assigned", "SAMPLE_ASSIGNED")
        await session.commit()
        posts = await _sent(expo)

    assert posts == []


@pytest.mark.asyncio
async def test_aFailedSendIsLoggedWithoutTheTokenOrMessage(caplog: pytest.LogCaptureFixture) -> None:
    session, expo = _session(), _FakeExpo(fail=True)

    with patch.object(notification_service.httpx, "AsyncClient", expo), caplog.at_level(logging.WARNING):
        await NotificationService(session).notify(uuid.uuid4(), "Specimen SMP-1 rejected", "SPECIMEN_REJECTED")
        await session.commit()
        await _sent(expo)

    [record] = [r for r in caplog.records if r.name == notification_service.__name__]
    assert record.getMessage() == "Expo push delivery failed for 1 message(s)"
    assert EXPO_TOKEN not in caplog.text
    assert "SMP-1" not in caplog.text


@pytest.mark.asyncio
async def test_theReturnedResultMessageNamesTheSampleOnly() -> None:
    service = NotificationService(AsyncMock())
    service.notify = AsyncMock()
    medtechId, resultId = uuid.uuid4(), uuid.uuid4()

    await service.notifyMedtechResultReturned(medtechId, resultId, "SMP-20261002-00042")

    service.notify.assert_awaited_once_with(
        userId=medtechId,
        message="Result for sample SMP-20261002-00042 was returned for correction.",
        notificationType="RESULT_RETURNED",
        entityId=resultId,
    )
