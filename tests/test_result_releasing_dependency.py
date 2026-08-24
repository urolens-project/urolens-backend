"""
Unit test — api/result_releasing.py's get_result_releasing_service

Regression test for a real bug found via a full-codebase Pyright scan:
NotificationService requires a SQLAlchemy AsyncSession (it writes
notification rows via SQLAlchemy Core), but this dependency factory used
to construct it with the same Supabase AsyncClient passed to
ResultReleasingService. Because NotificationService.notify() swallows
DB-write failures in a broad try/except, this silently broke result-release
notifications for both the patient and the ordering physician on every
DIGITAL release, with no visible error anywhere.

None of tests/integration/test_result_releasing.py's existing tests catch
this: they construct ResultReleasingService directly with a fully-mocked
NotificationService, bypassing this dependency factory entirely. This test
exercises the factory itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from supabase import AsyncClient

from src.api.result_releasing import getResultReleasingService
from src.services.notification_service import NotificationService
from src.services.result_releasing_service import ResultReleasingService


@pytest.mark.asyncio
async def test_notificationServiceGetsARealSqlalchemySessionNotTheSupabaseClient():
    supabaseClient = MagicMock(spec=AsyncClient)
    sqlalchemySession = MagicMock(spec=AsyncSession)

    _service = await getResultReleasingService(
        db=supabaseClient, sqlalchemyDb=sqlalchemySession
    )

    assert isinstance(_service, ResultReleasingService)
    assert _service.db is supabaseClient
    assert isinstance(_service._notificationService, NotificationService)
    assert _service._notificationService.db is sqlalchemySession
    assert _service._notificationService.db is not supabaseClient
