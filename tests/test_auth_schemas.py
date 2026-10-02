"""Unit tests — schemas/auth.py's LoginRequest (UROLENS-165).

A blank username or password previously passed Pydantic validation (plain
`str` accepts `""`) and fell through into the real login flow — a DB
lookup, then the generic 401 — instead of being rejected as a 422 naming
the field, as the UAC's "inline error identifying which field" requires.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.auth import LoginRequest


def test_validCredentialsAccepted() -> None:
    request = LoginRequest(username="jdoe", password="correct-horse-battery-staple")
    assert request.username == "jdoe"
    assert request.password == "correct-horse-battery-staple"


@pytest.mark.parametrize("field", ["username", "password"])
def test_emptyStringRejectedAtSchemaLevel(field: str) -> None:
    fields = {"username": "jdoe", "password": "secret"}
    fields[field] = ""
    with pytest.raises(ValidationError) as excInfo:
        LoginRequest(**fields)
    assert field in str(excInfo.value)


@pytest.mark.parametrize("field", ["username", "password"])
def test_whitespaceOnlyRejectedAtSchemaLevel(field: str) -> None:
    """`min_length=1` alone doesn't catch this — the not-blank validator does."""
    fields = {"username": "jdoe", "password": "secret"}
    fields[field] = "   "
    with pytest.raises(ValidationError) as excInfo:
        LoginRequest(**fields)
    assert field in str(excInfo.value)


def test_passwordValueIsNeverMutatedByValidation() -> None:
    """Unlike a free-text note field, a password's exact characters must
    reach verify_password unchanged — the validator must reject-only, never
    strip.
    """
    request = LoginRequest(username="jdoe", password="  leading and trailing spaces  ")
    assert request.password == "  leading and trailing spaces  "
