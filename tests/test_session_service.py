"""Unit tests — staff session lifetimes and token refresh (UROLENS-244).

`tokenExpiresAt`: a "keep signed in" token lasts the shift; a normal one 60
minutes, never past the shift. `refreshAccessToken`: same session and choice,
refused at the shift's end and for patient tokens; tokens issued before this
change (no `session_start`) count from their issue time.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from src.core.auth_service import sessionEndsAt, tokenExpiresAt
from src.core.config import settings
from src.services.session_service import refreshAccessToken

START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _decodeIgnoringTime(token: str) -> dict:
    # The signature is checked; the fixed test clock isn't (START may be in the past or future).
    return pyjwt.decode(
        token, settings.jwtSigningKey, algorithms=[settings.jwtAlgorithm],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )


def _claims(**overrides: object) -> dict:
    claims = {
        "user_id": str(uuid.uuid4()), "username": "medtech1", "role": "MEDTECH",
        "session_id": str(uuid.uuid4()), "session_start": int(START.timestamp()), "keep": False,
        "iat": int(START.timestamp()),
    }
    return {**claims, **overrides}


def test_aShiftIsTheConfiguredEightHoursAndATokenSixtyMinutes() -> None:
    assert (settings.jwtExpiryHours, settings.accessTokenExpireMinutes) == (8, 60)
    assert sessionEndsAt(START) == START + timedelta(hours=8)


@pytest.mark.parametrize(
    ("keep", "minutesIn", "expected"),
    [
        (True, 0, START + timedelta(hours=8)),
        (True, 300, START + timedelta(hours=8)),
        (False, 0, START + timedelta(minutes=60)),
        (False, 120, START + timedelta(minutes=180)),
        (False, 450, START + timedelta(hours=8)),  # 30 minutes left: capped
    ],
)
def test_whenATokenExpires(keep: bool, minutesIn: int, expected: datetime) -> None:
    assert tokenExpiresAt(START, keep, START + timedelta(minutes=minutesIn)) == expected


def test_refreshKeepsTheSessionAndTheKeepSignedInChoice() -> None:
    claims = _claims(keep=True)

    refreshed = refreshAccessToken(claims, now=START + timedelta(hours=2))

    new = _decodeIgnoringTime(refreshed.accessToken)
    assert (new["session_id"], new["user_id"], new["keep"]) == (claims["session_id"], claims["user_id"], True)
    assert new["session_start"] == claims["session_start"]
    assert refreshed.expiresAt == START + timedelta(hours=8)
    assert refreshed.sessionExpiresAt == START + timedelta(hours=8)


def test_refreshSlidesANormalTokenSixtyMinutesFromNow() -> None:
    refreshed = refreshAccessToken(_claims(), now=START + timedelta(hours=3))

    assert refreshed.expiresAt == START + timedelta(hours=4)
    assert refreshed.sessionExpiresAt == START + timedelta(hours=8)


@pytest.mark.parametrize("minutesPast", [0, 1])
def test_refreshIsRefusedOnceTheShiftIsOver(minutesPast: int) -> None:
    with pytest.raises(HTTPException) as excInfo:
        refreshAccessToken(_claims(), now=START + timedelta(hours=8, minutes=minutesPast))

    assert excInfo.value.status_code == 401
    assert excInfo.value.errorCode == "SESSION_EXPIRED"


def test_aTokenFromBeforeThisChangeCountsFromItsIssueTime() -> None:
    claims = _claims()
    del claims["session_start"], claims["keep"]

    refreshed = refreshAccessToken(claims, now=START + timedelta(hours=1))

    assert refreshed.sessionExpiresAt == START + timedelta(hours=8)
    assert refreshed.expiresAt == START + timedelta(hours=2)


@pytest.mark.parametrize("role", ["PATIENT", "patient"])
def test_patientTokensAreNotRefreshedHere(role: str) -> None:
    with pytest.raises(HTTPException) as excInfo:
        refreshAccessToken(_claims(role=role), now=START)

    assert excInfo.value.status_code == 403
    assert excInfo.value.errorCode == "ROLE_NOT_ALLOWED"
