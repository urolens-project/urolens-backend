import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from src.core.auth_service import (
    decodeJwt,
    hashPassword,
    incrementFailedAttempts,
    isSessionActive,
    issueJwt,
    resetFailedAttempts,
    verifyPassword,
)

MEDTECH_IDLE_TIMEOUT_MINUTES = 60
DEFAULT_IDLE_TIMEOUT_MINUTES = 30


class TestPasswordHashing:
    @pytest.mark.asyncio
    async def test_hashAndVerifyCorrectPassword(self):
        password = "secure_password_123"
        hashed = hashPassword(password)
        assert hashed != password
        assert await verifyPassword(password, hashed) is True

    @pytest.mark.asyncio
    async def test_verifyWrongPassword(self):
        password = "secure_password_123"
        hashed = hashPassword(password)
        assert await verifyPassword("wrong_password", hashed) is False

    def test_hashIsBcryptFormat(self):
        hashed = hashPassword("test")
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$")

    @pytest.mark.asyncio
    async def test_differentHashesForSamePassword(self):
        password = "password123"
        hash1 = hashPassword(password)
        hash2 = hashPassword(password)
        assert hash1 != hash2
        assert await verifyPassword(password, hash1) is True
        assert await verifyPassword(password, hash2) is True


class TestJWT:
    def test_issueAndDecodeRoundTrip(self):
        userId = uuid.uuid4()
        role = "PHYSICIAN"
        sessionId = uuid.uuid4()

        token = issueJwt(userId, "testuser", role, sessionId)
        claims = decodeJwt(token)

        assert claims["user_id"] == str(userId)
        assert claims["role"] == role
        assert claims["session_id"] == str(sessionId)
        assert "iat" in claims
        assert "exp" in claims
        assert claims["exp"] > claims["iat"]

    def test_tokenExpiryIs8Hours(self):
        userId = uuid.uuid4()
        role = "RECEPTIONIST"
        sessionId = uuid.uuid4()

        token = issueJwt(userId, "testuser", role, sessionId)
        claims = decodeJwt(token)

        iat = datetime.fromtimestamp(claims["iat"], tz=UTC)
        exp = datetime.fromtimestamp(claims["exp"], tz=UTC)
        delta = exp - iat
        assert delta == timedelta(hours=1)

    def test_decodeInvalidTokenRaises(self):
        with pytest.raises(jwt.PyJWTError):
            decodeJwt("not.a.valid.token")

    def test_decodeExpiredTokenRaises(self):
        now = datetime.now(UTC)
        expiredPayload = {
            "user_id": str(uuid.uuid4()),
            "role": "PATIENT",
            "session_id": str(uuid.uuid4()),
            "iat": now - timedelta(hours=10),
            "exp": now - timedelta(hours=2),
        }
        from src.core.config import settings

        expiredToken = jwt.encode(
            expiredPayload, settings.jwtSigningKey, algorithm=settings.jwtAlgorithm
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            decodeJwt(expiredToken)

    def test_decodeTokenWithWrongKeyRaises(self):
        userId = uuid.uuid4()
        sessionId = uuid.uuid4()
        payload = {
            "user_id": str(userId),
            "role": "RECEPTIONIST",
            "session_id": str(sessionId),
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=8),
        }
        token = jwt.encode(payload, "wrong-secret-key", algorithm="HS256")
        with pytest.raises(jwt.InvalidSignatureError):
            decodeJwt(token)


class TestFailedAttempts:
    @pytest.mark.asyncio
    async def test_incrementBelowThreshold(self):
        user = {"user_id": uuid.uuid4(), "failed_attempts": 2, "locked_at": None}
        with patch(
            "src.core.auth_service._getUserById",
            AsyncMock(return_value=user),
        ):
            mockExecute = AsyncMock()
            mockEq = MagicMock()
            mockEq.eq.return_value.execute = mockExecute
            mockUpdate = MagicMock()
            mockUpdate.update.return_value = mockEq
            mockTable = MagicMock()
            mockTable.table.return_value = mockUpdate
            with patch("src.core.auth_service.supabase", mockTable):
                await incrementFailedAttempts(user["user_id"])
                callArgs = mockUpdate.update.call_args
                assert callArgs is not None
                updateData = callArgs[0][0]
                assert updateData["failed_attempts"] == 3
                assert "locked_at" not in updateData

    @pytest.mark.asyncio
    async def test_incrementTriggersLockout(self):
        user = {"user_id": uuid.uuid4(), "failed_attempts": 4, "locked_at": None}
        with patch(
            "src.core.auth_service._getUserById",
            AsyncMock(return_value=user),
        ):
            mockExecute = AsyncMock()
            mockEq = MagicMock()
            mockEq.eq.return_value.execute = mockExecute
            mockUpdate = MagicMock()
            mockUpdate.update.return_value = mockEq
            mockTable = MagicMock()
            mockTable.table.return_value = mockUpdate
            with patch("src.core.auth_service.supabase", mockTable):
                await incrementFailedAttempts(user["user_id"])
                callArgs = mockUpdate.update.call_args
                updateData = callArgs[0][0]
                assert updateData["failed_attempts"] == 5
                assert "locked_at" in updateData

    @pytest.mark.asyncio
    async def test_resetFailedAttempts(self):
        mockExecute = AsyncMock()
        mockEq = MagicMock()
        mockEq.eq.return_value.execute = mockExecute
        mockUpdate = MagicMock()
        mockUpdate.update.return_value = mockEq
        mockTable = MagicMock()
        mockTable.table.return_value = mockUpdate
        with patch("src.core.auth_service.supabase", mockTable):
            await resetFailedAttempts(uuid.uuid4())


def _makeSessionSupabaseMock(selectData: dict | None) -> tuple[MagicMock, AsyncMock, AsyncMock]:
    """Mocks `supabase.table("sessions")`'s select-by-id and update-by-id
    chains separately, so a test can both feed `is_session_active`'s read
    and assert on whatever it subsequently wrote (a touch, or a revoke).
    """
    mock = MagicMock()
    selectExecute = AsyncMock(return_value=MagicMock(data=selectData))
    mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = (
        selectExecute
    )
    updateExecute = AsyncMock()
    mock.table.return_value.update.return_value.eq.return_value.execute = updateExecute
    return mock, selectExecute, updateExecute


class TestSessionActive:
    @pytest.mark.asyncio
    async def test_activeRecentlyTouchedSessionIsActive(self):
        recent = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": recent, "login_at": recent, "user_role": "RECEPTIONIST"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is True
        update.assert_awaited_once()  # UROLENS-167: touched as still-active

    @pytest.mark.asyncio
    async def test_closedSession(self):
        mock, _select, update = _makeSessionSupabaseMock({"is_active": False})
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is False
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nonexistentSession(self):
        mock, _select, update = _makeSessionSupabaseMock(None)
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is False
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sessionWithNoTimestampAtAllFailsClosed(self):
        """A session row active but with neither last_activity_at nor
        login_at (shouldn't happen post-UROLENS-167, but nothing to judge
        staleness against) is denied rather than treated as always-fresh.
        """
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": None, "login_at": None, "user_role": "RECEPTIONIST"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is False
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nonMedtechIdleFor31MinutesIsExpiredAndRevoked(self):
        stale = (datetime.now(UTC) - timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES + 1)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": stale, "login_at": stale, "user_role": "SUPERVISOR"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is False
        # closeSession() is what actually revokes it -- assert the real
        # effect (an update setting is_active False) rather than the name
        # of the helper that performed it.
        update.assert_awaited_once()
        assert mock.table.return_value.update.call_args.args[0]["is_active"] is False

    @pytest.mark.asyncio
    async def test_nonMedtechIdleFor29MinutesIsStillActive(self):
        fresh = (datetime.now(UTC) - timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES - 1)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": fresh, "login_at": fresh, "user_role": "SUPERVISOR"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is True
        # Still active -> touched (last_activity_at updated), never revoked.
        touchCall = mock.table.return_value.update.call_args.args[0]
        assert "last_activity_at" in touchCall
        assert "is_active" not in touchCall

    @pytest.mark.asyncio
    async def test_medtechGetsTheLongerSixtyMinuteTimeout(self):
        """The UAC gives MedTech a longer idle window than every other role
        -- 29 minutes idle (which would already expire SUPERVISOR etc.)
        must still be well within MedTech's own window.
        """
        age = (datetime.now(UTC) - timedelta(minutes=DEFAULT_IDLE_TIMEOUT_MINUTES + 1)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": age, "login_at": age, "user_role": "MEDTECH"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is True

    @pytest.mark.asyncio
    async def test_medtechIdleFor61MinutesIsExpired(self):
        stale = (datetime.now(UTC) - timedelta(minutes=MEDTECH_IDLE_TIMEOUT_MINUTES + 1)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": stale, "login_at": stale, "user_role": "MEDTECH"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is False

    @pytest.mark.asyncio
    async def test_fallsBackToLoginAtWhenLastActivityAtIsMissing(self):
        """A session row that predates the UROLENS-167 column (no
        `last_activity_at` yet) is judged by `login_at` until its first
        touch, not denied outright.
        """
        recentLogin = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        mock, _select, update = _makeSessionSupabaseMock(
            {"is_active": True, "last_activity_at": None, "login_at": recentLogin, "user_role": "ADMINISTRATOR"}
        )
        with patch("src.core.auth_service.supabase", mock):
            result = await isSessionActive(uuid.uuid4())

        assert result is True
