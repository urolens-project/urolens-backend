"""Integration tests — session timeout handling (UROLENS-245).

Through the real login, refresh and logout routes and the real session check
(on the in-memory Supabase fake from `test_auth_login`):
- login and refresh tell the app the role's idle limit (MedTech 60 min, others
  30) and the 2-minute warning;
- an idle session is signed out with `SESSION_IDLE` — every request says so,
  but the `SESSION_TIMED_OUT` audit row is written once — and "keep me signed
  in" sessions are no exception; a logged-out session still says `SESSION_ENDED`;
- activity is written at most once a minute;
- the app can report its own inactivity sign-out (`reason: "INACTIVITY"`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core import auth_service
from tests.integration.test_auth_login import FakeSupabase, auditEvents, seedUser

PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def client():
    fakeSb = FakeSupabase()
    # The folder's autouse fixture treats every session as active; these tests
    # need the real check against the fake's sessions table.
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
        patch("src.core.rbac.isSessionActive", auth_service.isSessionActive),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, fakeSb


async def _login(c: AsyncClient, fakeSb: FakeSupabase, role: str = "MEDTECH", **body: object) -> tuple[dict, dict]:
    seedUser(fakeSb, role=role)
    resp = await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": PASSWORD, **body})
    assert resp.status_code == 200, resp.text
    return resp.json(), fakeSb.store["sessions"][-1]


def _idleFor(session: dict, minutes: float) -> None:
    session["last_activity_at"] = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _anyRequest(c: AsyncClient, token: str) -> object:
    # Refresh is a light authenticated request that doesn't end the session.
    return await c.post("/api/v1/auth/refresh", headers=_bearer(token))


# ── The apps are told the limits ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "minutes"), [("MEDTECH", 60), ("SUPERVISOR", 30), ("RECEPTIONIST", 30)])
async def test_loginAndRefreshTellTheAppTheIdleLimitAndWarning(client, role: str, minutes: int) -> None:
    c, fakeSb = client

    body, _ = await _login(c, fakeSb, role=role)
    refreshed = (await _anyRequest(c, body["accessToken"])).json()

    for reply in (body, refreshed):
        assert (reply["idleTimeoutMinutes"], reply["idleWarningSeconds"]) == (minutes, 120)


# ── Idle sessions are signed out ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anIdleMedtechIsSignedOutAfterSixtyMinutes(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)
    _idleFor(session, 61)

    resp = await _anyRequest(c, body["accessToken"])

    assert resp.status_code == 401
    assert resp.json()["error"] == {
        "code": "SESSION_IDLE",
        "message": "You were signed out due to inactivity. Please log in again.",
    }
    assert session["is_active"] is False
    [timedOut] = auditEvents(fakeSb, "SESSION_TIMED_OUT")
    assert timedOut["detail_json"]["ended_by"] == "server"
    assert timedOut["detail_json"]["session_id"] == session["session_id"]


@pytest.mark.asyncio
async def test_aMedtechIsStillSignedInAtFiftyNineMinutes(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)
    _idleFor(session, 59)

    resp = await _anyRequest(c, body["accessToken"])

    assert resp.status_code == 200, resp.text
    assert session["is_active"] is True


@pytest.mark.asyncio
async def test_otherRolesAreSignedOutAfterThirtyMinutes(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb, role="SUPERVISOR")
    _idleFor(session, 31)

    resp = await _anyRequest(c, body["accessToken"])

    assert resp.json()["error"]["code"] == "SESSION_IDLE"


@pytest.mark.asyncio
async def test_keepSignedInSessionsAreSignedOutForInactivityToo(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb, keepSignedIn=True, client="mobile")
    _idleFor(session, 61)

    resp = await _anyRequest(c, body["accessToken"])

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_IDLE"


@pytest.mark.asyncio
async def test_everyLaterRequestAlsoSaysIdleButTheTimeoutIsAuditedOnce(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)
    _idleFor(session, 90)

    first = await _anyRequest(c, body["accessToken"])
    second = await _anyRequest(c, body["accessToken"])

    assert [r.json()["error"]["code"] for r in (first, second)] == ["SESSION_IDLE", "SESSION_IDLE"]
    assert len(auditEvents(fakeSb, "SESSION_TIMED_OUT")) == 1


@pytest.mark.asyncio
async def test_aLoggedOutSessionStillSaysEndedNotIdle(client) -> None:
    c, fakeSb = client
    body, _ = await _login(c, fakeSb)
    await c.post("/api/v1/auth/logout", headers=_bearer(body["accessToken"]))

    resp = await _anyRequest(c, body["accessToken"])

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_ENDED"


# ── Activity is written at most once a minute ─────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("minutesIdle", "written"), [(0.5, False), (2, True)])
async def test_activityIsWrittenAtMostOnceAMinute(client, minutesIdle: float, written: bool) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)
    _idleFor(session, minutesIdle)
    before = session["last_activity_at"]

    assert (await _anyRequest(c, body["accessToken"])).status_code == 200

    assert (session["last_activity_at"] != before) is written


# ── The app reports its own inactivity sign-out ───────────────────────────────

@pytest.mark.asyncio
async def test_anInactivitySignOutFromTheAppIsRecordedAsATimeout(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)

    resp = await c.post("/api/v1/auth/logout", json={"reason": "INACTIVITY"}, headers=_bearer(body["accessToken"]))

    assert resp.status_code == 204
    assert session["is_active"] is False
    [timedOut] = auditEvents(fakeSb, "SESSION_TIMED_OUT")
    assert timedOut["detail_json"]["ended_by"] == "client"
    assert auditEvents(fakeSb, "LOGOUT") == []


@pytest.mark.asyncio
async def test_aPlainLogoutIsStillALogout(client) -> None:
    c, fakeSb = client
    body, _ = await _login(c, fakeSb)

    resp = await c.post("/api/v1/auth/logout", headers=_bearer(body["accessToken"]))

    assert resp.status_code == 204
    assert len(auditEvents(fakeSb, "LOGOUT")) == 1
    assert auditEvents(fakeSb, "SESSION_TIMED_OUT") == []


@pytest.mark.asyncio
async def test_anUnknownLogoutReasonIsRefused(client) -> None:
    c, fakeSb = client
    body, session = await _login(c, fakeSb)

    resp = await c.post("/api/v1/auth/logout", json={"reason": "BORED"}, headers=_bearer(body["accessToken"]))

    assert resp.status_code == 422
    assert session["is_active"] is True


# ── End reasons ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aMissingSessionEndedRatherThanTimedOut(client) -> None:
    assert await auth_service.sessionEndReason("00000000-0000-0000-0000-000000000000") == "ENDED"


@pytest.mark.parametrize(("role", "minutes"), [("MEDTECH", 60), ("medtech", 60), ("PATIENT", 30), (None, 30)])
def test_idleLimitPerRole(role: str | None, minutes: int) -> None:
    assert auth_service.idleTimeoutMinutesForRole(role) == minutes
