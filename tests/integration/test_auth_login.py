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
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import patch

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
    seedUser(fakeSb, failed_attempts=5, locked_at="2026-09-08T00:00:00+00:00")

    resp = await c.post(
        "/api/v1/auth/login",
        json={"username": "medtech1", "password": "correct-horse-battery-staple"},
    )

    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == "ACCOUNT_LOCKED"


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
