"""Suite-wide fixtures and helpers.

The login rate limiter (`src.core.rate_limit`) keeps its counts in process
memory, so without a reset one test's login attempts would count against
the next test's — clear it around every test.

`makeSyncDb` is a session mock for `sync_service.pull`, shared by the sync
tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select

from src.core import rate_limit
from src.models.manual_override import ManualOverride
from src.models.result_return import ResultReturn


def makeSyncDb(
    reasons: list[tuple] | None = None, overrides: list[ManualOverride] | None = None
) -> AsyncMock:
    """A session for `sync_service.pull`: return-reason rows, then override rows."""

    def _execute(stmt: Select) -> MagicMock:
        entity = stmt.column_descriptions[0].get("entity")
        executeResult = MagicMock()
        executeResult.all.return_value = reasons or [] if entity is ResultReturn else []
        overrideRows = overrides or [] if entity is ManualOverride else []
        executeResult.scalars.return_value.all.return_value = overrideRows
        return executeResult

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


@pytest.fixture(autouse=True)
def clearLoginRateLimits():
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
    yield
    rate_limit._accountLimiter.clear()
    rate_limit._ipLimiter.clear()
