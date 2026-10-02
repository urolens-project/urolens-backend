"""Integration tests for POST /api/v1/auth/login and /logout.

Ported from the pre-consolidation `app.services.auth_service`-based version
of this test suite onto the current `src.core.auth_service` stack (same
logic, camelCase rename + directory unification — see changelog.md's
"Auth/RBAC + config consolidation" entry). No behavior changed between the
two versions of the login route itself; only the module paths and response
field names (`access_token` -> `accessToken`, `user_id` -> `userId`) did.

Architecture
------------
No real Supabase: `src.core.auth_service.supabase` and
`src.core.audit_logger.supabase` are both replaced with an in-memory fake
mimicking the postgrest chain. Password hashing and JWT signing/verification
run for real.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app
from src.core.auth_service import decodeJwt, hashPassword

# ── Fake Supabase (in-memory, stateful) ──────────────────────────────────────

class _FakeQuery:
    def __init__(self, store: dict, tableName: str):
        self.store = store
        self.tableName = tableName
        self._filters: dict = {}
        self._op: str | None = None
        self._payload: dict | None = None
        self._single = False

    def select(self, *_a, **_kw):
        self._op = self._op or "select"
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def maybe_single(self):
        self._single = True
        return self

    def insert(self, payload: dict):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload: dict):
        self._op = "update"
        self._payload = payload
        return self

    def _matched(self, rows: list[dict]) -> list[dict]:
        return [r for r in rows if all(r.get(k) == v for k, v in self._filters.items())]

    async def execute(self):
        rows = self.store.setdefault(self.tableName, [])

        if self._op == "insert":
            newRow = dict(self._payload or {})
            if self.tableName == "sessions" and "session_id" not in newRow:
                newRow["session_id"] = str(uuid.uuid4())  # mimics DB default
            rows.append(newRow)
            return SimpleNamespace(data=[newRow])

        if self._op == "update":
            matched = self._matched(rows)
            for r in matched:
                r.update(self._payload or {})
            return SimpleNamespace(data=matched)

        matched = self._matched(rows)
        if self._single:
            return SimpleNamespace(data=matched[0] if matched else None)
        return SimpleNamespace(data=matched)


class FakeSupabase:
    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self.store, name)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fakeSb():
    return FakeSupabase()


@pytest_asyncio.fixture
async def client(fakeSb):
    with (
        patch("src.core.auth_service.supabase", fakeSb),
        patch("src.core.audit_logger.supabase", fakeSb),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c, fakeSb


def seedUser(fakeSb: FakeSupabase, **overrides) -> dict:
    user = {
        "user_id": str(uuid.uuid4()),
        "username": "medtech1",
        "hashed_password": hashPassword("correct-horse-battery-staple"),
        "role": "MEDTECH",
        "failed_attempts": 0,
        "locked_at": None,
        "is_active": True,
    }
    user.update(overrides)
    fakeSb.store.setdefault("users", []).append(user)
    return user


def auditEvents(fakeSb: FakeSupabase, eventType: str) -> list[dict]:
    return [
        r for r in fakeSb.store.get("audit_logs", [])
        if r.get("event_type") == eventType
    ]


@pytest.mark.asyncio
async def test_login01_successfulLoginReturnsTokenAndRole(client):
    c, fakeSb = client
    user = seedUser(fakeSb)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "MEDTECH"
    assert body["userId"] == user["user_id"]
    assert body["accessToken"]

    claims = decodeJwt(body["accessToken"])
    assert claims["user_id"] == user["user_id"]
    assert claims["role"] == "MEDTECH"

    assert len(auditEvents(fakeSb, "LOGIN_SUCCESS")) == 1


@pytest.mark.asyncio
async def test_login02_wrongPasswordRejected(client):
    c, fakeSb = client
    user = seedUser(fakeSb)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "wrong-password"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    updated = fakeSb.store["users"][0]
    assert updated["failed_attempts"] == 1

    failed = auditEvents(fakeSb, "LOGIN_FAILED")
    assert len(failed) == 1
    assert failed[0]["user_id"] == user["user_id"]


@pytest.mark.asyncio
async def test_login03_unknownUsernameRejectedSameAsWrongPassword(client):
    c, fakeSb = client
    seedUser(fakeSb)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "does-not-exist", "password": "irrelevant"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert resp.json()["error"]["message"] == "Username or password is incorrect."

    failed = auditEvents(fakeSb, "LOGIN_FAILED")
    assert len(failed) == 1
    assert failed[0]["user_id"] is None


@pytest.mark.asyncio
async def test_login04_accountLocksOn5thFailedAttempt(client):
    c, fakeSb = client
    seedUser(fakeSb, failed_attempts=4, locked_at=None)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "wrong-password"},
    )

    assert resp.status_code == 401
    updated = fakeSb.store["users"][0]
    assert updated["failed_attempts"] == 5
    assert updated["locked_at"] is not None


@pytest.mark.asyncio
async def test_login05_lockedAccountRejectedEvenWithCorrectPassword(client):
    c, fakeSb = client
    lockedMinuteAgo = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    seedUser(fakeSb, failed_attempts=5, locked_at=lockedMinuteAgo)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == "ACCOUNT_LOCKED"


@pytest.mark.asyncio
async def test_login05b_lockExpiresAfterLockoutWindow(client):
    # UROLENS-222 / F-07: a lock no longer needs an administrator, so knowing
    # a username isn't enough to keep its owner locked out.
    c, fakeSb = client
    lockedLongAgo = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    seedUser(fakeSb, failed_attempts=5, locked_at=lockedLongAgo)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_login05c_wrongGuessWhileLockedGets423AndDoesNotExtendTheLock(client):
    # Before the fix a wrong guess re-stamped locked_at (endless lockout) and
    # got 401 where the right password got 423 (a "your guess was right" oracle).
    c, fakeSb = client
    lockedMinuteAgo = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    seedUser(fakeSb, failed_attempts=5, locked_at=lockedMinuteAgo)

    resp = await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": "wrong"})

    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == "ACCOUNT_LOCKED"
    user = fakeSb.store["users"][0]
    assert user["locked_at"] == lockedMinuteAgo
    assert user["failed_attempts"] == 5


@pytest.mark.asyncio
async def test_login05d_oneWrongGuessAfterExpiryLocksAgainImmediately(client):
    # The failure count is only reset by a successful login, so an expired
    # lock gives exactly one guess per window.
    c, fakeSb = client
    lockedLongAgo = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    seedUser(fakeSb, failed_attempts=5, locked_at=lockedLongAgo)

    resp = await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": "wrong"})

    assert resp.status_code == 401
    user = fakeSb.store["users"][0]
    assert user["failed_attempts"] == 6
    assert user["locked_at"] != lockedLongAgo  # re-locked just now


@pytest.mark.asyncio
async def test_login06_inactiveAccountRejected(client):
    c, fakeSb = client
    seedUser(fakeSb, is_active=False)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ACCOUNT_INACTIVE"


@pytest.mark.asyncio
async def test_login07_successfulLoginResetsFailedAttemptCounter(client):
    c, fakeSb = client
    seedUser(fakeSb, failed_attempts=3, locked_at=None)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 200
    updated = fakeSb.store["users"][0]
    assert updated["failed_attempts"] == 0
    assert updated["locked_at"] is None


@pytest.mark.asyncio
async def test_login08_logoutClosesSessionAndIsAudited(client):
    c, fakeSb = client
    seedUser(fakeSb)

    loginResp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )
    token = loginResp.json()["accessToken"]

    logoutResp = await c.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert logoutResp.status_code == 204

    session = fakeSb.store["sessions"][0]
    assert session["is_active"] is False
    assert "logout_at" in session

    assert len(auditEvents(fakeSb, "LOGOUT")) == 1


@pytest.mark.asyncio
async def test_login09_expiredJwtRejectedOnSubsequentRequest(client):
    from datetime import datetime, timedelta

    import jwt as pyjwt

    from src.core.config import settings

    c, fakeSb = client

    now = datetime.now(UTC)
    expiredPayload = {
        "user_id": str(uuid.uuid4()),
        "username": "medtech1",
        "role": "MEDTECH",
        "session_id": str(uuid.uuid4()),
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(minutes=1),
    }
    expiredToken = pyjwt.encode(expiredPayload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm)

    resp = await c.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {expiredToken}"},
    )

    assert resp.status_code == 401


# ── UROLENS-222: rate limiting and timing (audit F-06/F-07) ───────────────────

@pytest.mark.asyncio
async def test_login10_sixthAttemptInFiveMinutesIsRefusedWith429AndRetryAfter(client):
    c, fakeSb = client
    seedUser(fakeSb)
    for _ in range(5):
        await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": "wrong"})

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    assert int(resp.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_login11_unknownUsernameCostsOneBcryptCheckLikeAWrongPassword(client):
    c, fakeSb = client
    seedUser(fakeSb)

    with patch("src.api.auth.spendPasswordCheck", AsyncMock()) as spend:
        resp = await c.post("/api/v1/auth/login", json={"username": "nobody", "password": "guess"})

    assert resp.status_code == 401
    spend.assert_awaited_once_with("guess")


# ── UROLENS-165: blank-field validation + inactive-account message ordering ──

@pytest.mark.asyncio
async def test_login12_blankUsernameRejectedAtSchemaLevel(client):
    """Previously an empty username passed straight through to the real
    login flow (a DB lookup, then the generic 401) instead of being
    rejected as a validation error naming the field.
    """
    c, fakeSb = client
    seedUser(fakeSb)

    resp = await c.post("/api/v1/auth/login", json={"username": "", "password": "irrelevant"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "username" in resp.json()["error"]["details"]


@pytest.mark.asyncio
async def test_login13_whitespaceOnlyPasswordRejectedAtSchemaLevel(client):
    """`min_length=1` alone lets a string of spaces through — the
    not-blank validator is what actually catches this case.
    """
    c, fakeSb = client
    seedUser(fakeSb)

    resp = await c.post("/api/v1/auth/login", json={"username": "medtech1", "password": "   "})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "password" in resp.json()["error"]["details"]


@pytest.mark.asyncio
async def test_login14_inactiveAccountWithWrongPasswordStillGetsInactiveMessage(client):
    """The bug: the inactive check used to run only after a wrong-password
    guess had already raised the generic 401, so an inactive account's
    specific message never surfaced unless the password happened to be
    right. Now it's checked the same way as the lockout check — regardless
    of whether the password was also wrong.
    """
    c, fakeSb = client
    seedUser(fakeSb, is_active=False)

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "definitely-wrong"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    # A wrong guess against an inactive account still counts against the
    # failed-attempts counter, same as any other wrong guess (explicit
    # product decision — not a silent behavior change).
    updated = fakeSb.store["users"][0]
    assert updated["failed_attempts"] == 1
