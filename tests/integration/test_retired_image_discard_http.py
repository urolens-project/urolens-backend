"""HTTP-level tests — `POST /api/v1/images/{image_id}/discard` with the multi-field flag.

- Flag on: 410 `APP_UPDATE_REQUIRED` and nothing discarded, so an app that
  predates multi-field (or its queued `DISCARD_IMAGE`) is told to update.
- Flag off: the single-image discard answers exactly as before.
- The role check still comes first.
The service is replaced with a fake; its behaviour is covered by unit tests.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from main import app
from src.api.image import getImageRetakeService
from src.core.config import settings
from src.core.rbac import getCurrentUser

IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-0000000006a1")
PATH = f"/api/v1/images/{IMAGE_ID}/discard"


@pytest.fixture
def retakeService():
    """A fake retake service, removed after the test."""
    service = MagicMock()
    service.discardAndRetake = AsyncMock(return_value={
        "imageId": str(IMAGE_ID), "status": "DISCARDED", "discardedAt": "2026-10-27T08:00:00+00:00",
    })
    app.dependency_overrides[getImageRetakeService] = lambda: service
    try:
        yield service
    finally:
        app.dependency_overrides.pop(getImageRetakeService, None)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_withFieldsOnTheOldDiscardTellsTheAppToUpdate(
    asyncClient: AsyncClient, medtechToken: str, retakeService: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)

    response = await asyncClient.post(PATH, headers=_auth(medtechToken))

    assert response.status_code == 410
    assert response.json()["error"] == {"code": "APP_UPDATE_REQUIRED", "message": "Update UroLens to continue"}
    retakeService.discardAndRetake.assert_not_awaited()


@pytest.mark.asyncio
async def test_withFieldsOffTheOldDiscardAnswersAsBefore(
    asyncClient: AsyncClient, medtechToken: str, retakeService: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)

    response = await asyncClient.post(PATH, headers=_auth(medtechToken))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "imageId": str(IMAGE_ID), "status": "DISCARDED", "discardedAt": "2026-10-27T08:00:00+00:00",
    }
    retakeService.discardAndRetake.assert_awaited_once()


@pytest.mark.asyncio
async def test_anotherRoleIsForbiddenBeforeTheRetirementCheck(
    asyncClient: AsyncClient, retakeService: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(uuid.uuid4()), "role": "SUPERVISOR"}
    try:
        response = await asyncClient.post(PATH)
    finally:
        app.dependency_overrides.pop(getCurrentUser, None)

    assert response.status_code == 403
    retakeService.discardAndRetake.assert_not_awaited()
