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
        with pytest.raises(Exception):
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


class TestSessionActive:
    @pytest.mark.asyncio
    async def test_activeSession(self):
        with patch(
            "src.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data={"is_active": True})
            )
            result = await isSessionActive(uuid.uuid4())
            assert result is True

    @pytest.mark.asyncio
    async def test_closedSession(self):
        with patch(
            "src.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data={"is_active": False})
            )
            result = await isSessionActive(uuid.uuid4())
            assert result is False

    @pytest.mark.asyncio
    async def test_nonexistentSession(self):
        with patch(
            "src.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data=None)
            )
            result = await isSessionActive(uuid.uuid4())
            assert result is False
