"""Unit tests — login rate limiting, lockout expiry and timing (UROLENS-222, audit F-06/F-07).

- `SlidingWindowLimiter` with a fake clock;
- `enforceLoginRateLimit`: 5 attempts / 5 min per account, 30 / min per IP,
  429 `TOO_MANY_LOGIN_ATTEMPTS` with `Retry-After`;
- `isLockedOut`: a lock expires after `LOCKOUT_MINUTES`;
- `spendPasswordCheck`: an unknown account still costs one bcrypt check.
The suite-wide conftest clears the limiters between tests.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import auth_service, rate_limit
from src.core.auth_service import LOCKOUT_MINUTES, isLockedOut, spendPasswordCheck
from src.core.exceptions import TooManyRequestsException
from src.core.rate_limit import (
    SlidingWindowLimiter,
    clearLoginRateLimit,
    enforceLoginRateLimit,
)
from src.services import patient_auth_service


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ── SlidingWindowLimiter ──────────────────────────────────────────────────────

def test_limiterAllowsUpToTheLimitThenRefusesWithTheWaitTime():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=3, windowSeconds=60, clock=clock)

    assert [limiter.hit("k") for _ in range(3)] == [None, None, None]
    clock.now += 10
    assert limiter.hit("k") == pytest.approx(50)


def test_limiterWindowSlidesSoOldAttemptsExpire():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=2, windowSeconds=60, clock=clock)
    limiter.hit("k")
    limiter.hit("k")

    clock.now += 60

    assert limiter.hit("k") is None


def test_refusedAttemptsDoNotPushTheWindowFurtherOut():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=1, windowSeconds=60, clock=clock)
    limiter.hit("k")
    for _ in range(10):
        clock.now += 5
        assert limiter.hit("k") is not None  # hammering while blocked

    clock.now = 1060  # 60 s after the one recorded attempt

    assert limiter.hit("k") is None


def test_limiterKeysAreIndependentAndResettable():
    limiter = SlidingWindowLimiter(limit=1, windowSeconds=60, clock=_FakeClock())
    limiter.hit("a")

    assert limiter.hit("b") is None
    limiter.reset("a")
    assert limiter.hit("a") is None


def test_limiterKeepsAtMostMaxKeysWhenSprayedWithNewKeys():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=5, windowSeconds=60, clock=clock, maxKeys=100)

    for i in range(1000):
        limiter.hit(f"random-user-{i}")

    assert len(limiter._hits) <= 100


def test_expiredKeysAreEvictedBeforeActiveOnes():
    clock = _FakeClock()
    limiter = SlidingWindowLimiter(limit=1, windowSeconds=60, clock=clock, maxKeys=3)
    limiter.hit("old-1")
    limiter.hit("old-2")
    clock.now += 61
    limiter.hit("active")  # still inside its window, and over its limit of 1

    limiter.hit("new")  # full: triggers eviction

    assert "old-1" not in limiter._hits and "old-2" not in limiter._hits
    assert limiter.hit("active") is not None  # its limit survived the sweep


# ── enforceLoginRateLimit ─────────────────────────────────────────────────────

def test_sixthAttemptOnOneAccountIsRefusedWithRetryAfter():
    for _ in range(5):
        enforceLoginRateLimit("staff", "medtech1", "10.0.0.1")

    with pytest.raises(TooManyRequestsException) as excInfo:
        enforceLoginRateLimit("staff", "medtech1", "10.0.0.2")  # a different IP doesn't help

    assert excInfo.value.status_code == 429
    assert excInfo.value.errorCode == "TOO_MANY_LOGIN_ATTEMPTS"
    assert 1 <= int(excInfo.value.headers["Retry-After"]) <= 300


def test_accountLimitIgnoresCaseAndSurroundingWhitespace():
    for username in ["MedTech1", "medtech1", " MEDTECH1 ", "medtech1 ", "Medtech1"]:
        enforceLoginRateLimit("staff", username, "10.0.0.1")

    with pytest.raises(TooManyRequestsException):
        enforceLoginRateLimit("staff", "medtech1", "10.0.0.1")


def test_staffAndPatientLoginsNeverShareACounter():
    for _ in range(5):
        enforceLoginRateLimit("staff", "PAT-000001", "10.0.0.1")

    enforceLoginRateLimit("patient", "PAT-000001", "10.0.0.9")  # separate scope: allowed


def test_oneIpSprayingManyAccountsIsRefusedAfterThirty():
    for i in range(30):
        enforceLoginRateLimit("staff", f"user{i}", "10.0.0.66")

    with pytest.raises(TooManyRequestsException):
        enforceLoginRateLimit("staff", "user-next", "10.0.0.66")


def test_successfulLoginClearsTheAccountCounter():
    for _ in range(5):
        enforceLoginRateLimit("staff", "medtech1", "10.0.0.1")

    clearLoginRateLimit("staff", "MedTech1")

    enforceLoginRateLimit("staff", "medtech1", "10.0.0.1")  # allowed again


# ── Lockout expiry (F-07) ─────────────────────────────────────────────────────

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lockedAt", "expected"),
    [
        (None, False),
        ((NOW - timedelta(minutes=1)).isoformat(), True),
        ((NOW - timedelta(minutes=LOCKOUT_MINUTES, seconds=-1)).isoformat(), True),
        ((NOW - timedelta(minutes=LOCKOUT_MINUTES)).isoformat(), False),
        (NOW - timedelta(days=3), False),
    ],
)
def test_lockExpiresAfterTheLockoutWindow(lockedAt, expected):
    assert isLockedOut({"locked_at": lockedAt}, now=NOW) is expected


def test_lockoutWindowIsFifteenMinutes():
    assert LOCKOUT_MINUTES == 15


# ── Timing equalization (F-07) ────────────────────────────────────────────────

def test_dummyHashIsARealBcryptHashAtTheSameCostAsStoredPasswords():
    dummy = auth_service._TIMING_DUMMY_HASH
    assert dummy.startswith("$2b$12$") and len(dummy) == 60
    assert auth_service.hashPassword("x").split("$")[2] == "12"


@pytest.mark.asyncio
async def test_spendPasswordCheckRunsARealBcryptCheckThatFails():
    with patch.object(auth_service, "verifyPassword", wraps=auth_service.verifyPassword) as spy:
        await spendPasswordCheck("anything")

    spy.assert_awaited_once_with("anything", auth_service._TIMING_DUMMY_HASH)
    assert await auth_service.verifyPassword("anything", auth_service._TIMING_DUMMY_HASH) is False


# ── Patient login ─────────────────────────────────────────────────────────────

def _patientSupabase(patientRow: dict | None) -> MagicMock:
    query = MagicMock()
    for method in ("select", "eq", "maybe_single"):
        getattr(query, method).return_value = query
    query.execute = AsyncMock(return_value=MagicMock(data=patientRow))
    sb = MagicMock()
    sb.table.return_value = query
    return sb


@pytest.mark.asyncio
async def test_patientLoginIsRateLimitedBeforeAnyLookup():
    for _ in range(5):
        enforceLoginRateLimit("patient", "PAT-000001", "10.0.0.1")
    sb = _patientSupabase(None)

    with patch.object(patient_auth_service, "supabase", sb), \
         pytest.raises(TooManyRequestsException):
        await patient_auth_service.patientLogin(
            "PAT-000001", "guess", request=MagicMock(client=None, headers={})
        )

    sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_unknownPatientUidStillCostsOneBcryptCheck():
    sb = _patientSupabase(None)

    with patch.object(patient_auth_service, "supabase", sb), \
         patch.object(patient_auth_service, "spendPasswordCheck", AsyncMock()) as spend, \
         patch.object(patient_auth_service.audit_logger, "logPatientLoginFailed", AsyncMock()), \
         pytest.raises(Exception) as excInfo:
        await patient_auth_service.patientLogin(
            "PAT-999999", "guess", request=MagicMock(client=None, headers={})
        )

    assert excInfo.value.status_code == 401
    spend.assert_awaited_once_with("guess")


def test_limiterStateIsClearedBetweenTests():
    # Guards the suite-wide autouse fixture this file relies on.
    assert rate_limit._accountLimiter._hits == {}
    assert rate_limit._ipLimiter._hits == {}
