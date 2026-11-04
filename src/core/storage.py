"""Read access to microscopy images in Supabase Storage.

The `microscopy` bucket is private (SEC-0b): images are patient health data
under RA 10173, so clients never get a permanent public link. They get a
short-lived signed URL, issued only from an endpoint that has already passed
the route's auth/RBAC check.
"""
import logging

from .config import settings
from .supabase import supabase

logger = logging.getLogger(__name__)

SIGNED_IMAGE_URL_TTL_SECONDS = 3600
"""How long a signed image URL stays valid. A viewer that outlives it just
re-fetches the result detail, which issues a fresh one."""


async def signedImageUrl(storageKey: str | None) -> str | None:
    """Issue a time-limited URL for a specimen image in the private bucket.

    Returns:
        The signed URL, or `None` if there's no storage key or signing failed
        (e.g. the object is missing because its upload failed). A failure is
        logged, not raised — a missing image must not break the result view
        it is embedded in.
    """
    if not storageKey:
        return None
    try:
        signed = await supabase.storage.from_(settings.supabaseImageBucket).create_signed_url(
            storageKey, SIGNED_IMAGE_URL_TTL_SECONDS
        )
    except Exception:
        logger.warning("Could not sign image URL for %s", storageKey, exc_info=True)
        return None
    return signed.get("signedUrl")


async def signedImageUrls(storageKeys: list[str | None]) -> dict[str, str | None]:
    """Issue time-limited URLs for many specimen images with one Storage call.

    Same bucket and `SIGNED_IMAGE_URL_TTL_SECONDS` as `signedImageUrl`.

    Returns:
        A dict keyed by storage key. A key that is empty is skipped; a key that
        could not be signed (or every key, if the call failed) maps to `None`.
        Like `signedImageUrl` this never raises, and it never logs a URL (a
        signed URL is a bearer credential for the image).
    """
    keys = list(dict.fromkeys(key for key in storageKeys if key))
    if not keys:
        return {}
    signedByKey: dict[str, str | None] = dict.fromkeys(keys)
    try:
        signed = await supabase.storage.from_(settings.supabaseImageBucket).create_signed_urls(
            keys, SIGNED_IMAGE_URL_TTL_SECONDS
        )
    except Exception:
        logger.warning("Could not sign %d image URLs", len(keys), exc_info=True)
        return signedByKey
    for item in signed or []:
        path = item.get("path")
        if path in signedByKey and not item.get("error"):
            signedByKey[path] = item.get("signedURL") or item.get("signedUrl")
    return signedByKey
