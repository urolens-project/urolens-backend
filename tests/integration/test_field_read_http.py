"""HTTP-level tests — `GET /api/v1/fields/{field_id}` as the phone calls it.

The phone fetches a field it holds no boxes or image for (one captured on another
phone, or before a reinstall) while its result is still capturing (mobile PR #55
list, 2.2). Through the real route and service with a real MedTech token:
- the answer has what the phone stores: `detections`, the upright `imageWidth` and
  `imageHeight`, a signed `imageUrl`, and the field's Smart Diagnosis preview;
- while the result is `CAPTURING` nothing is written: the "opened" mark that the
  website's Mark as Checked needs is only recorded once the result awaits
  verification (so a phone fetching then would interfere; it must not);
- the image is only ever a signed URL, never a storage key.
The service's rules are in tests/test_field_read_paths.py, whose fake database this reuses.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from main import app
from src.core.config import settings
from src.core.database import getDb
from src.models.analysis_result import ResultStatus
from tests.integration.conftest import MEDTECH_USER_ID
from tests.test_field_read_paths import (
    FakeDb,
    _detections,
    _field,
    _result,
    _specimen,
    _updates,
)

SIGNED_URL = "https://storage.example/signed/field-1.jpg?token=short-lived"


@pytest.fixture
def fieldsOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)


def _install(db: FakeDb) -> None:
    async def _override() -> AsyncGenerator[FakeDb]:
        yield db

    app.dependency_overrides[getDb] = _override


@pytest.fixture
def capturingField(fieldsOn: None) -> AsyncGenerator[tuple[FakeDb, object]]:
    """One analysed field of the token's MedTech, its result still capturing."""
    field = _field(1, detections=_detections())
    field.smartDiagnosis = {"gout": {"level": "LOW"}}
    db = FakeDb(
        result=_result(ResultStatus.CAPTURING), specimen=_specimen(medtechId=MEDTECH_USER_ID),
        field=field, fields=[field],
    )
    _install(db)
    try:
        yield db, field
    finally:
        app.dependency_overrides.pop(getDb, None)


def _signed() -> object:
    return patch(
        "src.services.result_field_service.signedImageUrls",
        AsyncMock(side_effect=lambda keys: {key: SIGNED_URL for key in keys}),
    )


@pytest.mark.asyncio
async def test_thePhoneGetsWhatItStoresForAFieldItDoesNotHold(
    asyncClient: AsyncClient, medtechToken: str, capturingField: tuple
) -> None:
    _db, field = capturingField

    with _signed():
        response = await asyncClient.get(
            f"/api/v1/fields/{field.fieldId}", headers={"Authorization": f"Bearer {medtechToken}"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fieldId"] == str(field.fieldId)
    assert body["detections"] == _detections()
    assert (body["imageWidth"], body["imageHeight"]) == (800, 600)
    assert body["imageUrl"] == SIGNED_URL
    assert body["smartDiagnosisPreview"] == {"gout": {"level": "LOW"}}
    assert body["smartDiagnosisUnavailable"] is False


@pytest.mark.asyncio
async def test_fetchingWhileCapturingWritesNothing(
    asyncClient: AsyncClient, medtechToken: str, capturingField: tuple
) -> None:
    db, field = capturingField

    with _signed():
        await asyncClient.get(f"/api/v1/fields/{field.fieldId}", headers={"Authorization": f"Bearer {medtechToken}"})

    assert _updates(db) == []
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_onceTheResultAwaitsTheWebsiteAFetchCountsAsAnOpen(
    asyncClient: AsyncClient, medtechToken: str, fieldsOn: None
) -> None:
    # Why the phone must only fetch while capturing: here the same call is a website open.
    field = _field(1, detections=_detections())
    db = FakeDb(
        result=_result(ResultStatus.PENDING_CONFIRM), specimen=_specimen(medtechId=MEDTECH_USER_ID),
        field=field, fields=[field],
    )
    _install(db)
    try:
        with _signed():
            await asyncClient.get(
                f"/api/v1/fields/{field.fieldId}", headers={"Authorization": f"Bearer {medtechToken}"}
            )
    finally:
        app.dependency_overrides.pop(getDb, None)

    [update] = _updates(db)
    assert "last_opened_by" in update
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_withoutASignedUrlTheImageIsLeftOutNotExposed(
    asyncClient: AsyncClient, medtechToken: str, capturingField: tuple
) -> None:
    _db, field = capturingField

    with patch("src.services.result_field_service.signedImageUrls", AsyncMock(return_value={})):
        response = await asyncClient.get(
            f"/api/v1/fields/{field.fieldId}", headers={"Authorization": f"Bearer {medtechToken}"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["imageUrl"] is None
    assert "key-detail" not in response.text
