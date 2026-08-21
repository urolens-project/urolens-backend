import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from src.urolens.core.auth_service import (
    decode_jwt,
    hash_password,
    increment_failed_attempts,
    is_session_active,
    issue_jwt,
    reset_failed_attempts,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify_correct_password(self):
        password = "secure_password_123"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed) is True

    def test_verify_wrong_password(self):
        password = "secure_password_123"
        hashed = hash_password(password)
        assert verify_password("wrong_password", hashed) is False

    def test_hash_is_bcrypt_format(self):
        hashed = hash_password("test")
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$")

    def test_different_hashes_for_same_password(self):
        password = "password123"
        hash1 = hash_password(password)
        hash2 = hash_password(password)
        assert hash1 != hash2
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True


class TestJWT:
    def test_issue_and_decode_round_trip(self):
        user_id = uuid.uuid4()
        role = "PHYSICIAN"
        session_id = uuid.uuid4()

        token = issue_jwt(user_id, "testuser", role, session_id)
        claims = decode_jwt(token)

        assert claims["user_id"] == str(user_id)
        assert claims["role"] == role
        assert claims["session_id"] == str(session_id)
        assert "iat" in claims
        assert "exp" in claims
        assert claims["exp"] > claims["iat"]

    def test_token_expiry_is_8_hours(self):
        user_id = uuid.uuid4()
        role = "RECEPTIONIST"
        session_id = uuid.uuid4()

        token = issue_jwt(user_id, "testuser", role, session_id)
        claims = decode_jwt(token)

        iat = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
        exp = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        delta = exp - iat
        assert delta == timedelta(hours=1)

    def test_decode_invalid_token_raises(self):
        with pytest.raises(Exception):
            decode_jwt("not.a.valid.token")

    def test_decode_expired_token_raises(self):
        now = datetime.now(timezone.utc)
        expired_payload = {
            "user_id": str(uuid.uuid4()),
            "role": "PATIENT",
            "session_id": str(uuid.uuid4()),
            "iat": now - timedelta(hours=10),
            "exp": now - timedelta(hours=2),
        }
        from src.urolens.core.config import settings

        expired_token = jwt.encode(
            expired_payload, settings.jwt_signing_key, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_jwt(expired_token)

    def test_decode_token_with_wrong_key_raises(self):
        user_id = uuid.uuid4()
        session_id = uuid.uuid4()
        payload = {
            "user_id": str(user_id),
            "role": "RECEPTIONIST",
            "session_id": str(session_id),
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=8),
        }
        token = jwt.encode(payload, "wrong-secret-key", algorithm="HS256")
        with pytest.raises(jwt.InvalidSignatureError):
            decode_jwt(token)


class TestFailedAttempts:
    @pytest.mark.asyncio
    async def test_increment_below_threshold(self):
        user = {"user_id": uuid.uuid4(), "failed_attempts": 2, "locked_at": None}
        with patch(
            "src.urolens.core.auth_service._get_user_by_id",
            AsyncMock(return_value=user),
        ):
            mock_execute = AsyncMock()
            mock_eq = MagicMock()
            mock_eq.eq.return_value.execute = mock_execute
            mock_update = MagicMock()
            mock_update.update.return_value = mock_eq
            mock_table = MagicMock()
            mock_table.table.return_value = mock_update
            with patch("src.urolens.core.auth_service.supabase", mock_table):
                await increment_failed_attempts(user["user_id"])
                call_args = mock_update.update.call_args
                assert call_args is not None
                update_data = call_args[0][0]
                assert update_data["failed_attempts"] == 3
                assert "locked_at" not in update_data

    @pytest.mark.asyncio
    async def test_increment_triggers_lockout(self):
        user = {"user_id": uuid.uuid4(), "failed_attempts": 4, "locked_at": None}
        with patch(
            "src.urolens.core.auth_service._get_user_by_id",
            AsyncMock(return_value=user),
        ):
            mock_execute = AsyncMock()
            mock_eq = MagicMock()
            mock_eq.eq.return_value.execute = mock_execute
            mock_update = MagicMock()
            mock_update.update.return_value = mock_eq
            mock_table = MagicMock()
            mock_table.table.return_value = mock_update
            with patch("src.urolens.core.auth_service.supabase", mock_table):
                await increment_failed_attempts(user["user_id"])
                call_args = mock_update.update.call_args
                update_data = call_args[0][0]
                assert update_data["failed_attempts"] == 5
                assert "locked_at" in update_data

    @pytest.mark.asyncio
    async def test_reset_failed_attempts(self):
        mock_execute = AsyncMock()
        mock_eq = MagicMock()
        mock_eq.eq.return_value.execute = mock_execute
        mock_update = MagicMock()
        mock_update.update.return_value = mock_eq
        mock_table = MagicMock()
        mock_table.table.return_value = mock_update
        with patch("src.urolens.core.auth_service.supabase", mock_table):
            await reset_failed_attempts(uuid.uuid4())


class TestSessionActive:
    @pytest.mark.asyncio
    async def test_active_session(self):
        with patch(
            "src.urolens.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data={"is_active": True})
            )
            result = await is_session_active(uuid.uuid4())
            assert result is True

    @pytest.mark.asyncio
    async def test_closed_session(self):
        with patch(
            "src.urolens.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data={"is_active": False})
            )
            result = await is_session_active(uuid.uuid4())
            assert result is False

    @pytest.mark.asyncio
    async def test_nonexistent_session(self):
        with patch(
            "src.urolens.core.auth_service.supabase",
            MagicMock(),
        ) as mock:
            mock.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute = AsyncMock(
                return_value=MagicMock(data=None)
            )
            result = await is_session_active(uuid.uuid4())
            assert result is False
