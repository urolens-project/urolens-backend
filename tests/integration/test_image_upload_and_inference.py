# tests/integration/test_image_upload_and_inference.py
"""
Integration test — T2.7: Image upload → AI inference triggered → result in DB

Covers:
  - Happy path: valid JPEG, inference succeeds, DB row populated.
  - Format error: unsupported MIME type returns 422.
  - Resolution error: image below 640×480 returns 422.
  - AI inference failure: upload still returns 201, result.status=FAILED.
  - Discard flow: POST /discard transitions status, audit log written.
  - Audit log events: IMAGE_UPLOADED and IMAGE_DISCARDED verified.

Fixtures (provided by conftest.py):
  - async_client: httpx AsyncClient wired to the FastAPI app
  - db_session: AsyncSession with rollback-per-test isolation
  - medtech_token: JWT for a MEDTECH user
  - test_specimen: a Specimen row in ASSIGNED state
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession

from src.urolens.models.analysis_result import AnalysisResult, ResultStatus
from src.urolens.models.audit_log import AuditLog
from src.urolens.models.image import Image, ImageStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_jpeg(width: int = 800, height: int = 600) -> bytes:
    """Generate a minimal valid JPEG in memory."""
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


def _mock_infer_result() -> MagicMock:
    m = MagicMock()
    m.to_dict.return_value = FAKE_AI_FINDINGS
    return m


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_valid_image_triggers_inference_and_persists_result(
    async_client: AsyncClient,
    db_session: AsyncSession,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """Happy path: valid 800×600 JPEG → inference runs → AnalysisResult created."""
    jpeg_bytes = _make_jpeg(800, 600)

    with patch("src.urolens.services.ai_integration_service.infer", return_value=_mock_infer_result()):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == ResultStatus.PENDING_REVIEW
    assert body["ai_findings"] == FAKE_AI_FINDINGS
    assert body["specimen_id"] == str(test_specimen)

    # Verify DB state
    result = await db_session.get(AnalysisResult, uuid.UUID(body["id"]))
    assert result is not None
    assert result.ai_findings == FAKE_AI_FINDINGS
    assert result.status == ResultStatus.PENDING_REVIEW

    image = await db_session.get(Image, result.image_id)
    assert image is not None
    assert image.status == ImageStatus.PROCESSED
    assert image.width_px == 800
    assert image.height_px == 600


@pytest.mark.asyncio
async def test_upload_unsupported_format_returns_422(
    async_client: AsyncClient,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """GIF uploads must be rejected with 422 before any DB write."""
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
    async_client: AsyncClient,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """A 320×240 JPEG is below the 640×480 minimum and must be rejected."""
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
async def test_upload_succeeds_when_ai_inference_fails(
    async_client: AsyncClient,
    db_session: AsyncSession,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """
    AI failure must not break the upload. The endpoint returns 201 and the
    AnalysisResult row is created with status=FAILED and ai_findings=None.
    """
    jpeg_bytes = _make_jpeg()

    with patch(
        "src.urolens.services.ai_integration_service.infer",
        side_effect=RuntimeError("GPU out of memory"),
    ):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == ResultStatus.FAILED
    assert body["ai_findings"] is None

    result = await db_session.get(AnalysisResult, uuid.UUID(body["id"]))
    assert result is not None
    assert result.status == ResultStatus.FAILED

    # Image status should also reflect the failure
    image = await db_session.get(Image, result.image_id)
    assert image.status == ImageStatus.FAILED


@pytest.mark.asyncio
async def test_audit_log_image_uploaded_is_written(
    async_client: AsyncClient,
    db_session: AsyncSession,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """Every successful upload must produce an IMAGE_UPLOADED audit log entry."""
    from sqlalchemy import select

    jpeg_bytes = _make_jpeg()
    with patch("src.urolens.services.ai_integration_service.infer", return_value=_mock_infer_result()):
        response = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )
    assert response.status_code == 201

    image_id = (await db_session.get(AnalysisResult, uuid.UUID(response.json()["id"]))).image_id
    logs = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "IMAGE_UPLOADED",
                AuditLog.entity_id == image_id,
            )
        )
    ).scalars().all()

    assert len(logs) == 1, "Expected exactly one IMAGE_UPLOADED audit log entry"
    assert logs[0].entity_type == "image"


@pytest.mark.asyncio
async def test_discard_image_transitions_status_and_writes_audit_log(
    async_client: AsyncClient,
    db_session: AsyncSession,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """
    POST /images/{id}/discard must:
      1. Set image.status = DISCARDED
      2. Write an IMAGE_DISCARDED audit log entry
    """
    from sqlalchemy import select

    # Upload an image first
    jpeg_bytes = _make_jpeg()
    with patch("src.urolens.services.ai_integration_service.infer", return_value=_mock_infer_result()):
        upload_resp = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )
    image_id = (
        await db_session.get(AnalysisResult, uuid.UUID(upload_resp.json()["id"]))
    ).image_id

    # Discard
    discard_resp = await async_client.post(
        f"/api/v1/images/{image_id}/discard",
        headers={"Authorization": f"Bearer {medtech_token}"},
    )
    assert discard_resp.status_code == 200
    assert discard_resp.json()["status"] == ImageStatus.DISCARDED

    # DB state
    await db_session.refresh(await db_session.get(Image, image_id))
    image = await db_session.get(Image, image_id)
    assert image.status == ImageStatus.DISCARDED
    assert image.discarded_at is not None

    # Audit log
    logs = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.event_type == "IMAGE_DISCARDED",
                AuditLog.entity_id == image_id,
            )
        )
    ).scalars().all()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_discard_already_discarded_image_returns_conflict(
    async_client: AsyncClient,
    medtech_token: str,
    test_specimen: uuid.UUID,
) -> None:
    """Discarding an already-discarded image must return 409 Conflict."""
    jpeg_bytes = _make_jpeg()
    with patch("src.urolens.services.ai_integration_service.infer", return_value=_mock_infer_result()):
        upload_resp = await async_client.post(
            "/api/v1/images/upload",
            headers={"Authorization": f"Bearer {medtech_token}"},
            files={"file": ("specimen.jpg", jpeg_bytes, "image/jpeg")},
            data={"specimen_id": str(test_specimen)},
        )

    from src.urolens.models.analysis_result import AnalysisResult
    image_id = (
        await (  # noqa — can't await in list comp
            # We need a fresh session here; using async_client's injected session
            async_client.app.state.db_session.get(AnalysisResult, uuid.UUID(upload_resp.json()["id"]))
        )
    ).image_id

    headers = {"Authorization": f"Bearer {medtech_token}"}
    await async_client.post(f"/api/v1/images/{image_id}/discard", headers=headers)
    second = await async_client.post(f"/api/v1/images/{image_id}/discard", headers=headers)

    assert second.status_code == 409