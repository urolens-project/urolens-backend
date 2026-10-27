"""HTTP-level tests — `GET /api/v1/sync/pull` keeps its JSON without `resultFields`
while the multi-field flag is off, and carries it while on. The service is
replaced with a fake; its behaviour is covered by `tests/test_sync_result_fields.py`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from src.services import sync_service

_EMPTY = {"created": [], "updated": [], "deleted": []}
_TABLES = {"specimens": _EMPTY, "queueAssignments": _EMPTY, "analysisResults": _EMPTY, "manualOverrides": _EMPTY}


async def _get(asyncClient: AsyncClient, token: str, payload: dict) -> object:
    with patch.object(sync_service, "pull", AsyncMock(return_value=payload)):
        return await asyncClient.get("/api/v1/sync/pull", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_withoutFieldsTheResponseHasNoResultFieldsKey(asyncClient: AsyncClient, medtechToken: str) -> None:
    payload = {"timestamp": "2026-10-27T08:00:00+00:00", "changes": _TABLES}

    response = await _get(asyncClient, medtechToken, payload)

    assert response.status_code == 200, response.text
    assert response.json() == payload


@pytest.mark.asyncio
async def test_withFieldsTheResponseCarriesThem(asyncClient: AsyncClient, medtechToken: str) -> None:
    fields = {"created": [{"fieldId": "f1", "status": "ACCEPTED"}], "updated": [], "deleted": []}
    payload = {"timestamp": "2026-10-27T08:00:00+00:00", "changes": {**_TABLES, "resultFields": fields}}

    response = await _get(asyncClient, medtechToken, payload)

    assert response.status_code == 200, response.text
    assert response.json()["changes"]["resultFields"] == fields
