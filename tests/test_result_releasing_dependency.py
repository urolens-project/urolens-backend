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

from src.urolens.api.result_releasing import get_result_releasing_service
from src.urolens.services.notification_service import NotificationService
from src.urolens.services.result_releasing_service import ResultReleasingService


@pytest.mark.asyncio
async def test_notification_service_gets_a_real_sqlalchemy_session_not_the_supabase_client():
    supabase_client = MagicMock(spec=AsyncClient)
    sqlalchemy_session = MagicMock(spec=AsyncSession)

    service = await get_result_releasing_service(
        db=supabase_client, sqlalchemy_db=sqlalchemy_session
    )

    assert isinstance(service, ResultReleasingService)
    assert service.db is supabase_client
    assert isinstance(service.notification_service, NotificationService)
    assert service.notification_service.db is sqlalchemy_session
    assert service.notification_service.db is not supabase_client
