"""Integration tests — mobile login, staying signed in, and refresh (UROLENS-244).

Through the real `/auth/login`, `/auth/refresh` and `/auth/logout` routes,
with real password hashing and JWT signing, on the in-memory Supabase fake
from `test_auth_login`:
- `client: "mobile"` lets only MedTechs in (`ROLE_NOT_ALLOWED`, no session),
  and never reveals a role to a wrong password;
- `keepSignedIn` makes the token last the shift; otherwise 60 minutes;
- `POST /auth/refresh` renews the token for the same session, never past the
  shift's end, and stops working once the session ends;
- refusals say why (`SESSION_EXPIRED`, `SESSION_ENDED`);
- blank or oversized credentials are refused.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core import auth_service
from src.core.auth_service import decodeJwt
from src.core.config import settings
from tests.integration.test_auth_login import (
    FakeSupabase,
    auditEvents,
    seedUser,
    standInDb,
)

PASSWORD = "correct-horse-battery-staple"


@pytest_asyncio.fixture
async def client():
    fakeSb = FakeSupabase()
    # The folder's autouse fixture treats every session as active; these tests
    # need the real check (against the fake's sessions table) to see logouts.
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
        patch("src.core.rbac.isSessionActive", auth_service.isSessionActive),
        standInDb(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, fakeSb


async def _login(c: AsyncClient, **body: object) -> object:
    return await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": PASSWORD, **body})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(sessionId: str, sessionStart: datetime, exp: datetime, role: str = "MEDTECH", keep: bool = False) -> str:
    now = datetime.now(UTC)
    claims = {
        "user_id": str(uuid.uuid4()), "username": "medtech1", "role": role, "session_id": sessionId,
        "session_start": int(sessionStart.timestamp()), "keep": keep, "iat": now, "exp": exp,
    }
    return pyjwt.encode(claims, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)


def _activeSession(fakeSb: FakeSupabase) -> str:
    sessionId = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    # Since UROLENS-167 a session needs an activity time, or it counts as idle.
    fakeSb.store.setdefault("sessions", []).append(
        {"session_id": sessionId, "is_active": True, "login_at": now, "last_activity_at": now, "user_role": "MEDTECH"}
    )
    return sessionId


def _near(value: str, expected: datetime, seconds: int = 5) -> bool:
    return abs(datetime.fromisoformat(value) - expected) <= timedelta(seconds=seconds)


# ── The mobile app is for MedTechs ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aMedtechCanLogIntoTheMobileApp(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)

    resp = await _login(c, client="mobile")

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "MEDTECH"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["SUPERVISOR", "RECEPTIONIST", "PHYSICIAN", "ADMINISTRATOR"])
async def test_otherRolesAreRefusedOnMobileBeforeAnySessionIsCreated(client, role: str) -> None:
    c, fakeSb = client
    seedUser(fakeSb, role=role)

    resp = await _login(c, client="mobile")

    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "ROLE_NOT_ALLOWED",
        "message": "This app is for Medical Technologists. Please use the web portal.",
    }
    assert fakeSb.store.get("sessions", []) == []
    assert auditEvents(fakeSb, "LOGIN_SUCCESS") == []
    assert len(auditEvents(fakeSb, "ACCESS_DENIED")) == 1


@pytest.mark.asyncio
async def test_aWrongPasswordOnMobileNeverRevealsTheRole(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb, role="SUPERVISOR")

    resp = await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": "wrong", "client": "mobile"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"client": "web"}], ids=["no-client", "web"])
async def test_theWebLetsEveryStaffRoleIn(client, extra: dict) -> None:
    c, fakeSb = client
    seedUser(fakeSb, role="SUPERVISOR")

    resp = await _login(c, **extra)

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "SUPERVISOR"


# ── Staying signed in ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aNormalLoginLastsSixtyMinutesWithinAnEightHourSession(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)
    before = datetime.now(UTC)

    body = (await _login(c)).json()

    assert _near(body["expiresAt"], before + timedelta(minutes=60))
    assert _near(body["sessionExpiresAt"], before + timedelta(hours=8))
    claims = decodeJwt(body["accessToken"])
    assert claims["keep"] is False
    assert datetime.fromtimestamp(claims["exp"], UTC) == datetime.fromisoformat(body["expiresAt"])


@pytest.mark.asyncio
async def test_keepSignedInLastsTheWholeShift(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)
    before = datetime.now(UTC)

    body = (await _login(c, keepSignedIn=True, client="mobile")).json()

    assert body["expiresAt"] == body["sessionExpiresAt"]
    assert _near(body["expiresAt"], before + timedelta(hours=8))
    claims = decodeJwt(body["accessToken"])
    assert claims["keep"] is True
    assert datetime.fromtimestamp(claims["exp"], UTC) == datetime.fromisoformat(body["sessionExpiresAt"])


# ── Refresh ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refreshRenewsTheTokenForTheSameSession(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)
    login = (await _login(c)).json()

    resp = await c.post("/api/v1/auth/refresh", headers=_bearer(login["accessToken"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    old, new = decodeJwt(login["accessToken"]), decodeJwt(body["accessToken"])
    assert (new["session_id"], new["user_id"], new["role"]) == (old["session_id"], old["user_id"], old["role"])
    assert new["session_start"] == old["session_start"]
    assert body["sessionExpiresAt"] == login["sessionExpiresAt"]
    assert _near(body["expiresAt"], datetime.now(UTC) + timedelta(minutes=60))
    assert len(fakeSb.store["sessions"]) == 1  # no new session


@pytest.mark.asyncio
async def test_refreshNeverGoesPastTheEndOfTheShift(client) -> None:
    c, fakeSb = client
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=7, minutes=30)
    token = _token(_activeSession(fakeSb), start, exp=datetime.now(UTC) + timedelta(minutes=10))

    body = (await c.post("/api/v1/auth/refresh", headers=_bearer(token))).json()

    shiftEnd = start + timedelta(hours=8)
    assert datetime.fromisoformat(body["expiresAt"]) == shiftEnd  # 30 minutes left, not 60
    assert datetime.fromisoformat(body["sessionExpiresAt"]) == shiftEnd


@pytest.mark.asyncio
async def test_refreshStopsWorkingOnceTheSessionIsLoggedOut(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)
    token = (await _login(c)).json()["accessToken"]
    await c.post("/api/v1/auth/logout", headers=_bearer(token))

    resp = await c.post("/api/v1/auth/refresh", headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_ENDED"


@pytest.mark.asyncio
async def test_anExpiredTokenCannotBeRefreshed(client) -> None:
    c, fakeSb = client
    start = datetime.now(UTC) - timedelta(hours=2)
    token = _token(_activeSession(fakeSb), start, exp=datetime.now(UTC) - timedelta(seconds=1))

    resp = await c.post("/api/v1/auth/refresh", headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"] == {"code": "SESSION_EXPIRED", "message": "Your session has expired. Please log in again."}


@pytest.mark.asyncio
async def test_aPatientTokenCannotBeRefreshedHere(client) -> None:
    c, fakeSb = client
    token = _token(_activeSession(fakeSb), datetime.now(UTC), exp=datetime.now(UTC) + timedelta(minutes=5), role="PATIENT")

    resp = await c.post("/api/v1/auth/refresh", headers=_bearer(token))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ROLE_NOT_ALLOWED"


# ── Refusals say why ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aGarbledTokenIsAPlainUnauthorized(client) -> None:
    c, _ = client

    resp = await c.post("/api/v1/auth/logout", headers=_bearer("not-a-token"))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_anEndedSessionSaysSo(client) -> None:
    c, fakeSb = client
    sessionId = _activeSession(fakeSb)
    fakeSb.store["sessions"][0]["is_active"] = False
    token = _token(sessionId, datetime.now(UTC), exp=datetime.now(UTC) + timedelta(minutes=5))

    resp = await c.post("/api/v1/auth/logout", headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"] == {"code": "SESSION_ENDED", "message": "Your session has ended. Please log in again."}


# ── Credentials are checked for blanks and size ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"username": "", "password": PASSWORD},
        {"username": "   ", "password": PASSWORD},
        {"username": "medtech1", "password": ""},
        {"username": "medtech1", "password": "x" * 257},
        {"username": "u" * 151, "password": PASSWORD},
        {"username": "medtech1", "password": PASSWORD, "client": "tablet"},
    ],
    ids=["blank-username", "spaces-username", "blank-password", "long-password", "long-username", "unknown-client"],
)
async def test_badCredentialsAreRefusedBeforeAnyLookup(client, body: dict) -> None:
    c, fakeSb = client

    resp = await c.post("/api/v1/auth/login", json=body)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert auditEvents(fakeSb, "LOGIN_FAILED") == []


@pytest.mark.asyncio
async def test_spacesAroundTheUsernameAreIgnored(client) -> None:
    c, fakeSb = client
    seedUser(fakeSb)

    resp = await c.post("/api/v1/auth/login", json={"username": "  medtech1  ", "password": PASSWORD})

    assert resp.status_code == 200, resp.text
