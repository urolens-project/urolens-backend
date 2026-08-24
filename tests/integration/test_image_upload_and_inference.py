"""Integration tests — T2.7: Image upload → AI inference hook → discard/retake flow

Covers
------
- Happy path: valid JPEG, 201 returned, analysis_results upserted.
- Format error: unsupported MIME type → 422.
- Resolution error: image below 640×480 → 422.
- AI inference triggered when urolens_ai package is importable.
- AI failure is non-fatal: upload still returns 201, findings stay empty.
- Discard flow: 200 returned, service validates state.
- Double-discard: 409 Conflict.

Architecture
------------
No real DB: Supabase client is replaced per-test with an AsyncMock.
See conftest.py for fixture details.
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage

from main import app
from src.core.database import getDb
from src.models.analysis_result import AnalysisResult
from tests.integration.conftest import (
    TEST_IMAGE_ID,
    TEST_SPECIMEN_ID,
    _makeSbMock,
)

# ── Image helpers ─────────────────────────────────────────────────────────────

def _makeJpeg(width: int = 800, height: int = 600) -> bytes:
    img = PILImage.new("RGB", (width, height), color=(120, 180, 240))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


FAKE_AI_FINDINGS = {
    "RBC": 12,
    "WBC": 4,
    "Epithelial": 0,
    "Bacteria": 0,
    "Crystals": 0,
    "Casts": 0,
}


# ── SQLAlchemy AsyncSession mock (for the upload endpoint's Depends(getDb)) ────
#
# handleUpload() queries for a previous image/specimen/existing result (all
# absent on a first upload -- every .scalar_one_or_none() returns None to
# take the "create new" path), then constructs a real AnalysisResult(...)
# instance itself (not mocked) and flushes it. Unlike a real session, flush()
# on a mock never runs SQLAlchemy's column-default machinery, so `resultId`
# (mapped_column(..., default=uuid.uuid4)) is assigned by hand here, mirroring
# what a real flush would do.

def _makeSqlDbMock() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()  # real AsyncSession.add() is synchronous, not a coroutine
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=executeResult)

    async def _flush(objs):
        for obj in objs:
            if isinstance(obj, AnalysisResult) and obj.resultId is None:
                obj.resultId = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    return db


@pytest.fixture
def mockSqlDb():
    """Overrides Depends(getDb) app-wide for the duration of one test, so the
    upload endpoint's SQLAlchemy calls hit this mock instead of a real
    Postgres connection.
    """
    db = _makeSqlDbMock()

    async def _override():
        yield db

    app.dependency_overrides[getDb] = _override
    try:
        yield db
    finally:
        app.dependency_overrides.pop(getDb, None)


# ── Upload tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uploadValidImageReturns201(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """Valid 800×600 JPEG → 201 with PENDING_CONFIRM status."""
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg(800, 600)

    with (
        patch("src.services.ai_integration_service.sb", sbMock),
        patch("src.services.image_retake_service.sb", sbMock),
    ):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["specimenId"] == str(testSpecimen)
    assert "resultId" in body
    assert "imageId" in body


@pytest.mark.asyncio
async def test_uploadUnsupportedFormatReturns422(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """GIF upload must be rejected with 422 — no Supabase calls needed."""
    gifBytes = b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"

    response = await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtechToken}"},
        files={"file": ("specimen.gif", gifBytes, "image/gif")},
        data={"specimenId": str(testSpecimen)},
    )

    assert response.status_code == 422
    assert "format" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_uploadBelowMinimumResolutionReturns422(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """320×240 JPEG is below 640×480 minimum — must return 422."""
    smallJpeg = _makeJpeg(320, 240)

    response = await asyncClient.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtechToken}"},
        files={"file": ("tiny.jpg", smallJpeg, "image/jpeg")},
        data={"specimenId": str(testSpecimen)},
    )

    assert response.status_code == 422
    assert "resolution" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_uploadTriggersAiInferenceWhenPackageAvailable(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """When urolens_ai is installed, _try_run_inference is called and the findings
    are included in the response (ai_findings populated).
    """
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg()

    # `infer` is the callable; `infer(raw_bytes)` returns the inference object
    # _runInference() reads `inferenceResult.particles` (a dict), not `.to_dict()`.
    mockInferFn = MagicMock()
    mockInferFn.return_value.particles = FAKE_AI_FINDINGS

    with (
        patch("src.services.ai_integration_service.sb", sbMock),
        patch("src.services.image_retake_service.sb", sbMock),
        # Simulate urolens_ai being installed by patching the import inside _try_run_inference
        patch("builtins.__import__", _makeImportMock("urolens_ai", "infer", mockInferFn)),
    ):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["aiFindings"] == FAKE_AI_FINDINGS


@pytest.mark.asyncio
async def test_uploadSucceedsWhenAiInferenceFails(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
    mockSqlDb,
) -> None:
    """AI failure must not break the upload. The endpoint returns 201 and
    ai_findings is None (the result row stays PENDING_CONFIRM with empty findings).
    """
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])
    jpegBytes = _makeJpeg()

    def _raisingInfer(_bytes):
        raise RuntimeError("GPU out of memory")

    with (
        patch("src.services.ai_integration_service.sb", sbMock),
        patch("src.services.image_retake_service.sb", sbMock),
        patch("builtins.__import__", _makeImportMock("urolens_ai", "infer", _raisingInfer)),
    ):
        response = await asyncClient.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtechToken}"},
            files={"file": ("specimen.jpg", jpegBytes, "image/jpeg")},
            data={"specimenId": str(testSpecimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["aiFindings"] is None


# ── Discard tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discardActiveImageReturns200(
    asyncClient,
    medtechToken: str,
    testSpecimen: uuid.UUID,
) -> None:
    """POST /images/{id}/discard on an ACTIVE image → 200 with DISCARDED status."""
    activeImage = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "ACTIVE",
    }
    sbMock = _makeSbMock(imagesRows=[activeImage], analysisRows=[])

    with patch("src.services.image_retake_service.sb", sbMock):
        response = await asyncClient.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtechToken}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "DISCARDED"
    assert body["imageId"] == str(TEST_IMAGE_ID)
    assert body["discardedAt"] is not None


@pytest.mark.asyncio
async def test_discardAlreadyDiscardedImageReturns409(
    asyncClient,
    medtechToken: str,
) -> None:
    """Discarding an already-DISCARDED image must return 409 Conflict."""
    discardedImage = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "DISCARDED",
    }
    sbMock = _makeSbMock(imagesRows=[discardedImage], analysisRows=[])

    with patch("src.services.image_retake_service.sb", sbMock):
        response = await asyncClient.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtechToken}"},
        )

    assert response.status_code == 409
    assert "discarded" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_discardNonexistentImageReturns404(
    asyncClient,
    medtechToken: str,
) -> None:
    """Discarding an image that doesn't exist must return 404."""
    sbMock = _makeSbMock(imagesRows=[], analysisRows=[])

    with patch("src.services.image_retake_service.sb", sbMock):
        response = await asyncClient.post(
            f"/api/v1/images/{uuid.uuid4()}/discard",
            headers={"Authorization": f"Bearer {medtechToken}"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_discardReplacedImageReturns409(
    asyncClient,
    medtechToken: str,
) -> None:
    """Discarding a REPLACED image must return 409 Conflict."""
    replacedImage = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "REPLACED",
    }
    sbMock = _makeSbMock(imagesRows=[replacedImage], analysisRows=[])

    with patch("src.services.image_retake_service.sb", sbMock):
        response = await asyncClient.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtechToken}"},
        )

    assert response.status_code == 409


# ── Helpers ───────────────────────────────────────────────────────────────────

def _makeImportMock(moduleName: str, attr: str, value):
    """Returns a replacement for builtins.__import__ that intercepts imports of
    `module_name` and returns a mock module exposing `attr` = `value`.

    All other imports fall through to the real __import__.
    """
    import builtins
    realImport = builtins.__import__

    def _patchedImport(name, *args, **kwargs):
        if name == moduleName:
            mod = MagicMock()
            setattr(mod, attr, value)
            return mod
        return realImport(name, *args, **kwargs)

    return _patchedImport
