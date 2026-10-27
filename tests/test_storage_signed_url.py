"""Unit tests — src.core.storage.signedImageUrl (SEC-0b: microscopy bucket is
private; clients only ever get short-lived signed URLs). The Supabase client
is mocked; no network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import storage
from src.core.config import settings

STORAGE_KEY = "specimens/00000000-0000-0000-0000-000000000051/images/00000000-0000-0000-0000-000000000052.jpg"
SIGNED_URL = f"https://example.supabase.co/storage/v1/object/sign/microscopy/{STORAGE_KEY}?token=abc"


def _mockSupabase(signResult=None, signError: Exception | None = None) -> MagicMock:
    bucket = MagicMock()
    bucket.create_signed_url = AsyncMock(
        return_value=signResult, side_effect=signError
    )
    sb = MagicMock()
    sb.storage.from_.return_value = bucket
    return sb


@pytest.mark.asyncio
async def test_signedImageUrlSignsTheKeyInTheImageBucketWithTheConfiguredTtl():
    sb = _mockSupabase({"signedURL": SIGNED_URL, "signedUrl": SIGNED_URL})
    with patch.object(storage, "supabase", sb):
        url = await storage.signedImageUrl(STORAGE_KEY)

    assert url == SIGNED_URL
    sb.storage.from_.assert_called_once_with(settings.supabaseImageBucket)
    sb.storage.from_.return_value.create_signed_url.assert_awaited_once_with(
        STORAGE_KEY, storage.SIGNED_IMAGE_URL_TTL_SECONDS
    )


@pytest.mark.asyncio
async def test_signedImageUrlNeverReturnsAPermanentPublicLink():
    sb = _mockSupabase({"signedURL": SIGNED_URL, "signedUrl": SIGNED_URL})
    with patch.object(storage, "supabase", sb):
        url = await storage.signedImageUrl(STORAGE_KEY)

    assert "/object/public/" not in url


def test_signedImageUrlTtlIsShortLived():
    # Guards against someone "fixing" expiring links by making them permanent.
    assert 0 < storage.SIGNED_IMAGE_URL_TTL_SECONDS <= 24 * 3600


@pytest.mark.asyncio
@pytest.mark.parametrize("storageKey", [None, ""])
async def test_signedImageUrlReturnsNoneWithoutCallingStorageWhenThereIsNoKey(storageKey):
    sb = _mockSupabase()
    with patch.object(storage, "supabase", sb):
        url = await storage.signedImageUrl(storageKey)

    assert url is None
    sb.storage.from_.assert_not_called()


@pytest.mark.asyncio
async def test_signedImageUrlReturnsNoneInsteadOfRaisingWhenSigningFails(caplog):
    # e.g. the object is missing because its upload failed (upload failure is
    # non-fatal in ai_integration_service) — the result view must still load.
    sb = _mockSupabase(signError=RuntimeError("Object not found"))
    with patch.object(storage, "supabase", sb):
        url = await storage.signedImageUrl(STORAGE_KEY)

    assert url is None
    assert "Could not sign image URL" in caplog.text


@pytest.mark.asyncio
async def test_signedImageUrlReturnsNoneWhenStorageSignsNothing():
    sb = _mockSupabase({"signedURL": None, "signedUrl": None})
    with patch.object(storage, "supabase", sb):
        url = await storage.signedImageUrl(STORAGE_KEY)

    assert url is None
