"""
Integration tests — T2.7: Image upload → AI inference hook → discard/retake flow

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

from tests.integration.conftest import (
    MEDTECH_USER_ID,
    TEST_IMAGE_ID,
    TEST_RESULT_ID,
    TEST_SPECIMEN_ID,
    _make_sb_mock,
)


# ── Image helpers ─────────────────────────────────────────────────────────────

def _make_jpeg(width: int = 800, height: int = 600) -> bytes:
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


# ── Upload tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_valid_image_returns_201(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """Valid 800×600 JPEG → 201 with PENDING_CONFIRM status."""
    sb_mock = _make_sb_mock(images_rows=[], analysis_rows=[])
    jpeg_bytes = _make_jpeg(800, 600)

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["specimen_id"] == str(test_specimen)
    assert "result_id" in body
    assert "image_id" in body


@pytest.mark.asyncio
async def test_upload_unsupported_format_returns_422(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """GIF upload must be rejected with 422 — no Supabase calls needed."""
    gif_bytes = b"GIF89a\x01\x00\x01\x00\x00\xff\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00;"

    response = await async_client.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtech_token}"},
        files={"file": ("specimen.gif", gif_bytes, "image/gif")},
        data={"specimen_id": str(test_specimen)},
    )

    assert response.status_code == 422
    assert "format" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_upload_below_minimum_resolution_returns_422(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """320×240 JPEG is below 640×480 minimum — must return 422."""
    small_jpeg = _make_jpeg(320, 240)

    response = await async_client.post(
        "/api/v1/images/upload",
        headers={"Authorization": f"Bearer {medtech_token}"},
        files={"file": ("tiny.jpg", small_jpeg, "image/jpeg")},
        data={"specimen_id": str(test_specimen)},
    )

    assert response.status_code == 422
    assert "resolution" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_upload_triggers_ai_inference_when_package_available(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """
    When urolens_ai is installed, _try_run_inference is called and the findings
    are included in the response (ai_findings populated).
    """
    sb_mock = _make_sb_mock(images_rows=[], analysis_rows=[])
    jpeg_bytes = _make_jpeg()

    # `infer` is the callable; `infer(raw_bytes)` returns the inference object
    mock_infer_fn = MagicMock()
    mock_infer_fn.return_value.to_dict.return_value = FAKE_AI_FINDINGS

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
        # Simulate urolens_ai being installed by patching the import inside _try_run_inference
        patch("builtins.__import__", _make_import_mock("urolens_ai", "infer", mock_infer_fn)),
    ):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["ai_findings"] == FAKE_AI_FINDINGS


@pytest.mark.asyncio
async def test_upload_succeeds_when_ai_inference_fails(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """
    AI failure must not break the upload. The endpoint returns 201 and
    ai_findings is None (the result row stays PENDING_CONFIRM with empty findings).
    """
    sb_mock = _make_sb_mock(images_rows=[], analysis_rows=[])
    jpeg_bytes = _make_jpeg()

    def _raising_infer(_bytes):
        raise RuntimeError("GPU out of memory")

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
        patch("builtins.__import__", _make_import_mock("urolens_ai", "infer", _raising_infer)),
    ):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_CONFIRM"
    assert body["ai_findings"] is None


# ── Discard tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discard_active_image_returns_200(
    async_client,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """POST /images/{id}/discard on an ACTIVE image → 200 with DISCARDED status."""
    active_image = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "ACTIVE",
    }
    sb_mock = _make_sb_mock(images_rows=[active_image], analysis_rows=[])

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        response = await async_client.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtech_token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "DISCARDED"
    assert body["image_id"] == str(TEST_IMAGE_ID)
    assert body["discarded_at"] is not None


@pytest.mark.asyncio
async def test_discard_already_discarded_image_returns_409(
    async_client,
    medtech_token: str,
) -> None:
    """Discarding an already-DISCARDED image must return 409 Conflict."""
    discarded_image = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "DISCARDED",
    }
    sb_mock = _make_sb_mock(images_rows=[discarded_image], analysis_rows=[])

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        response = await async_client.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtech_token}"},
        )

    assert response.status_code == 409
    assert "discarded" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_discard_nonexistent_image_returns_404(
    async_client,
    medtech_token: str,
) -> None:
    """Discarding an image that doesn't exist must return 404."""
    sb_mock = _make_sb_mock(images_rows=[], analysis_rows=[])

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        response = await async_client.post(
            f"/api/v1/images/{uuid.uuid4()}/discard",
            headers={"Authorization": f"Bearer {medtech_token}"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_discard_replaced_image_returns_409(
    async_client,
    medtech_token: str,
) -> None:
    """Discarding a REPLACED image must return 409 Conflict."""
    replaced_image = {
        "image_id": str(TEST_IMAGE_ID),
        "specimen_id": str(TEST_SPECIMEN_ID),
        "status": "REPLACED",
    }
    sb_mock = _make_sb_mock(images_rows=[replaced_image], analysis_rows=[])

    with (
        patch("src.urolens.api.image.sb", sb_mock),
        patch("src.urolens.services.image_retake_service.sb", sb_mock),
    ):
        response = await async_client.post(
            f"/api/v1/images/{TEST_IMAGE_ID}/discard",
            headers={"Authorization": f"Bearer {medtech_token}"},
        )

    assert response.status_code == 409


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_import_mock(module_name: str, attr: str, value):
    """
    Returns a replacement for builtins.__import__ that intercepts imports of
    `module_name` and returns a mock module exposing `attr` = `value`.

    All other imports fall through to the real __import__.
    """
    import builtins
    real_import = builtins.__import__

    def _patched_import(name, *args, **kwargs):
        if name == module_name:
            mod = MagicMock()
            setattr(mod, attr, value)
            return mod
        return real_import(name, *args, **kwargs)

    return _patched_import
