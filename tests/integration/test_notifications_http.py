"""Integration tests — viewing and managing notifications (UROLENS-248).

Through the real `/notifications` and `/users/push-token` routes, on a mock
session whose statements are compiled and checked:
- the list stays a plain newest-first list by default, and takes `limit` (the
  bell's preview), `before` (paging the full page) and `unreadOnly`;
- the unread count counts all unread, for the bell badge;
- mark one / mark all read; another user's notification is `NOTIFICATION_NOT_FOUND`;
- every query is scoped to the caller's own user ID, for every role;
- registering a device takes it away from whoever had it before, and only an
  Expo token is accepted;
- a manual logout stops pushes to the device; an inactivity sign-out
  (`reason: "INACTIVITY"`, UROLENS-245) doesn't.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.notification import Notification
from tests.integration.test_auth_login import FakeSupabase, seedUser, standInDb

USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
NOTIFICATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
EXPO_TOKEN = "ExponentPushToken[abc123]"


def _token(role: str = "MEDTECH", userId: uuid.UUID = USER_ID) -> dict[str, str]:
    now = datetime.now(UTC)
    payload = {
        "user_id": str(userId),
        "role": role,
        "session_id": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)}"}


def _notification(minutesAgo: int = 0, isRead: bool = False) -> Notification:
    return Notification(
        notificationId=uuid.uuid4(),
        userId=USER_ID,
        message="New specimen assigned: SMP-20261002-00001",
        notificationType="SAMPLE_ASSIGNED",
        entityId=uuid.uuid4(),
        isRead=isRead,
        createdAt=datetime.now(UTC) - timedelta(minutes=minutesAgo),
    )


def _result(rows: list | None = None, scalar: object = None, rowcount: int = 1) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows or []
    result.scalar_one.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    result.rowcount = rowcount
    return result


def _sql(db: AsyncMock, call: int = 0) -> str:
    stmt = db.execute.call_args_list[call].args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest_asyncio.fixture
async def client():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result())

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, db
    finally:
        app.dependency_overrides.pop(getDb, None)


# ── The list ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_theListIsStillAPlainNewestFirstListOfTheCallersOwn(client) -> None:
    c, db = client
    rows = [_notification(1), _notification(5, isRead=True)]
    db.execute.return_value = _result(rows)

    resp = await c.get("/api/v1/notifications", headers=_token())

    assert resp.status_code == 200
    assert [n["notificationId"] for n in resp.json()] == [str(r.notificationId) for r in rows]
    assert resp.json()[1]["isRead"] is True
    sql = _sql(db)
    assert f"notifications.user_id = '{USER_ID}'" in sql
    assert "ORDER BY notifications.created_at DESC, notifications.notification_id DESC" in sql
    assert sql.endswith("LIMIT 50")
    assert "is_read" not in sql.split("WHERE")[1]


@pytest.mark.asyncio
async def test_thePreviewAsksForAFewUnreadOnes(client) -> None:
    c, db = client

    resp = await c.get("/api/v1/notifications?limit=5&unreadOnly=true", headers=_token())

    assert resp.status_code == 200
    sql = _sql(db)
    assert "notifications.is_read IS false" in sql
    assert sql.endswith("LIMIT 5")


@pytest.mark.asyncio
async def test_theNextPageStartsAfterTheLastOneShown(client) -> None:
    c, db = client
    cursorTime = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    db.execute = AsyncMock(side_effect=[_result(scalar=cursorTime), _result([_notification(90)])])

    resp = await c.get(f"/api/v1/notifications?before={NOTIFICATION_ID}", headers=_token())

    assert resp.status_code == 200
    cursorSql, pageSql = _sql(db, 0), _sql(db, 1)
    assert f"notifications.notification_id = '{NOTIFICATION_ID}'" in cursorSql
    assert f"notifications.user_id = '{USER_ID}'" in cursorSql
    assert "(notifications.created_at, notifications.notification_id) < " in pageSql
    assert f"'{NOTIFICATION_ID}'" in pageSql
    assert f"notifications.user_id = '{USER_ID}'" in pageSql


@pytest.mark.asyncio
async def test_pagingFromSomeoneElsesNotificationIsNotFound(client) -> None:
    c, db = client
    db.execute.return_value = _result(scalar=None)

    resp = await c.get(f"/api/v1/notifications?before={NOTIFICATION_ID}", headers=_token())

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"
    assert db.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101])
async def test_aPageSizeOutsideOneToAHundredIsRefused(client, limit: int) -> None:
    c, db = client

    resp = await c.get(f"/api/v1/notifications?limit={limit}", headers=_token())

    assert resp.status_code == 422
    db.execute.assert_not_awaited()


# ── The badge ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["MEDTECH", "SUPERVISOR", "RECEPTIONIST", "PHYSICIAN", "PATIENT"])
async def test_everyRoleGetsItsOwnUnreadCount(client, role: str) -> None:
    c, db = client
    db.execute.return_value = _result(scalar=3)

    resp = await c.get("/api/v1/notifications/unread-count", headers=_token(role))

    assert resp.status_code == 200
    assert resp.json() == {"unreadCount": 3}
    sql = _sql(db)
    assert "count(*)" in sql
    assert f"notifications.user_id = '{USER_ID}'" in sql
    assert "notifications.is_read IS false" in sql


@pytest.mark.asyncio
async def test_signedOutCallersGetNothing(client) -> None:
    c, db = client

    resp = await c.get("/api/v1/notifications/unread-count")

    assert resp.status_code in (401, 403)
    db.execute.assert_not_awaited()


# ── Marking read ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openingANotificationMarksItRead(client) -> None:
    c, db = client

    resp = await c.patch(f"/api/v1/notifications/{NOTIFICATION_ID}/read", headers=_token())

    assert resp.status_code == 204
    sql = _sql(db)
    assert "UPDATE notifications SET is_read=true" in sql
    assert f"notifications.notification_id = '{NOTIFICATION_ID}'" in sql
    assert f"notifications.user_id = '{USER_ID}'" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_someoneElsesNotificationIsNotFoundAndUntouched(client) -> None:
    c, db = client
    db.execute.return_value = _result(rowcount=0)

    resp = await c.patch(f"/api/v1/notifications/{NOTIFICATION_ID}/read", headers=_token())

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_markAllReadOnlyTouchesTheCallersUnreadOnes(client) -> None:
    c, db = client

    resp = await c.patch("/api/v1/notifications/read-all", headers=_token())

    assert resp.status_code == 204
    sql = _sql(db)
    assert "UPDATE notifications SET is_read=true" in sql
    assert f"notifications.user_id = '{USER_ID}'" in sql
    assert "notifications.is_read IS false" in sql
    db.commit.assert_awaited_once()


# ── The device that gets the pushes ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_registeringADeviceTakesItFromWhoeverHadItBefore(client) -> None:
    c, db = client

    resp = await c.post("/api/v1/users/push-token", json={"token": f"  {EXPO_TOKEN} "}, headers=_token())

    assert resp.status_code == 204
    takeAway, assign = _sql(db, 0), _sql(db, 1)
    assert "UPDATE users SET expo_push_token=NULL" in takeAway
    assert f"users.expo_push_token = '{EXPO_TOKEN}'" in takeAway
    assert f"users.user_id != '{USER_ID}'" in takeAway
    assert "UPDATE users SET expo_push_token=" in assign
    assert f"expo_push_token='{EXPO_TOKEN}'" in assign
    assert f"users.user_id = '{USER_ID}'" in assign
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "not-a-token", "ExponentPushToken[unclosed", "x" * 300])
async def test_onlyAnExpoTokenIsAccepted(client, token: str) -> None:
    c, db = client

    resp = await c.post("/api/v1/users/push-token", json={"token": token}, headers=_token())

    assert resp.status_code == 422
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_aManualLogoutStopsPushesToTheDevice() -> None:
    fakeSb = FakeSupabase()
    user = seedUser(fakeSb)
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
        standInDb() as db,
    ):
        db.execute = AsyncMock(return_value=_result())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            login = await c.post(
                "/api/v1/auth/login",
                json={"username": "medtech1", "password": "correct-horse-battery-staple"},
            )
            resp = await c.post(
                "/api/v1/auth/logout", headers={"Authorization": f"Bearer {login.json()['accessToken']}"}
            )

    assert resp.status_code == 204
    sql = _sql(db)
    assert "UPDATE users SET expo_push_token=NULL" in sql
    assert f"users.user_id = '{user['user_id']}'" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_anInactivitySignOutKeepsTheDevice() -> None:
    fakeSb = FakeSupabase()
    seedUser(fakeSb)
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
        standInDb() as db,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            login = await c.post(
                "/api/v1/auth/login",
                json={"username": "medtech1", "password": "correct-horse-battery-staple"},
            )
            resp = await c.post(
                "/api/v1/auth/logout",
                json={"reason": "INACTIVITY"},
                headers={"Authorization": f"Bearer {login.json()['accessToken']}"},
            )

    assert resp.status_code == 204
    assert fakeSb.store["sessions"][-1]["is_active"] is False
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aFailureToForgetTheDeviceStillLogsOut() -> None:
    fakeSb = FakeSupabase()
    seedUser(fakeSb)
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
        standInDb() as db,
    ):
        db.execute = AsyncMock(side_effect=RuntimeError("database unreachable"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            login = await c.post(
                "/api/v1/auth/login",
                json={"username": "medtech1", "password": "correct-horse-battery-staple"},
            )
            resp = await c.post(
                "/api/v1/auth/logout", headers={"Authorization": f"Bearer {login.json()['accessToken']}"}
            )

    assert resp.status_code == 204
    assert fakeSb.store["sessions"][-1]["is_active"] is False
