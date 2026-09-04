"""
Unit test — api/result_releasing.py's get_result_releasing_service

Originally a regression test for a real bug found via a full-codebase
Pyright scan: NotificationService requires a SQLAlchemy AsyncSession (it
writes notification rows via SQLAlchemy Core), but the dependency factory
used to construct it with the Supabase AsyncClient ResultReleasingService
used for everything else — silently breaking result-release notifications
on every DIGITAL release.

Now that ResultReleasingService itself is on SQLAlchemy `AsyncSession` (no
Supabase client involved at all), that specific mismatch is structurally
impossible: the factory hands the exact same session to both. This test
now pins that invariant instead — both must reference the one request-scoped
session, not merely "a" SQLAlchemy session.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.result_releasing import getResultReleasingService
from src.services.notification_service import NotificationService
from src.services.result_releasing_service import ResultReleasingService


@pytest.mark.asyncio
async def test_notificationServiceSharesTheSameSessionAsTheReleasingService():
    session = MagicMock(spec=AsyncSession)

    _service = await getResultReleasingService(db=session)

    assert isinstance(_service, ResultReleasingService)
    assert _service.db is session
    assert isinstance(_service._notificationService, NotificationService)
    assert _service._notificationService.db is session
