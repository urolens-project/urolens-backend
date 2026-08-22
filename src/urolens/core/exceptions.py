"""Domain-specific `HTTPException` subclasses, each carrying a machine-readable
`error_code` alongside the HTTP status/detail. Routes should raise these
directly rather than a bare `HTTPException`, per the response-envelope
convention (no raw `detail=str(e)` to the client)."""
from __future__ import annotations

from fastapi import HTTPException, status


class ImageFormatError(HTTPException):
    """422 — an uploaded image isn't an accepted format (JPEG/PNG). `error_code`
    is `"INVALID_IMAGE_FORMAT"`."""

    def __init__(self, message: str = "Unsupported image format. Accepted: JPEG, PNG."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.error_code = "INVALID_IMAGE_FORMAT"


class ImageResolutionError(HTTPException):
    """422 — an uploaded image is below the minimum 640×480 resolution.
    `error_code` is `"INVALID_IMAGE_RESOLUTION"`."""

    def __init__(self, message: str = "Image resolution is below the required 640×480 minimum."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.error_code = "INVALID_IMAGE_RESOLUTION"


class StorageError(HTTPException):
    """503 — storing/retrieving a file (e.g. Supabase storage) failed.
    `error_code` is `"STORAGE_ERROR"`."""

    def __init__(self, message: str = "Failed to store image. Please try again."):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=message,
        )
        self.error_code = "STORAGE_ERROR"


class NotFoundError(HTTPException):
    """404 — the requested resource doesn't exist. `error_code` is `"NOT_FOUND"`."""

    def __init__(self, message: str = "Resource not found."):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=message,
        )
        self.error_code = "NOT_FOUND"


class ConflictError(HTTPException):
    """409 — the resource exists but is in a state that conflicts with the
    request. `error_code` is `"CONFLICT"`."""

    def __init__(self, message: str = "Resource state conflict."):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )
        self.error_code = "CONFLICT"


class SpecimenNotFoundError(NotFoundError):
    """404 — a specimen lookup by ID found nothing. `error_code` is
    `"SPECIMEN_NOT_FOUND"`.

    Args:
        specimen_id: included in the default message when given; the message
            falls back to a generic "not found" if omitted.
    """

    def __init__(self, specimen_id: str | None = None):
        msg = f"Specimen {specimen_id} not found." if specimen_id else "Specimen not found."
        super().__init__(message=msg)
        self.error_code = "SPECIMEN_NOT_FOUND"


# ── Service-layer exceptions (used by service classes, not raised as HTTP directly) ──

class NotFoundException(HTTPException):
    """404, with a caller-supplied `error_code` (default `"NOT_FOUND"`) — used
    by service classes rather than raised as HTTP directly by routers.

    Args:
        code: sets `error_code` on the exception; distinct from `NotFoundError`,
            which hardcodes it.
    """

    def __init__(self, code: str = "NOT_FOUND", message: str = "Resource not found."):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=message)
        self.error_code = code


class ConflictException(HTTPException):
    """409, with a caller-supplied `error_code` (default `"CONFLICT"`) — used
    by service classes rather than raised as HTTP directly by routers.

    Args:
        code: sets `error_code` on the exception; distinct from `ConflictError`,
            which hardcodes it.
    """

    def __init__(self, code: str = "CONFLICT", message: str = "Resource state conflict."):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=message)
        self.error_code = code


class UnprocessableException(HTTPException):
    """422, with a caller-supplied `error_code` (default `"UNPROCESSABLE"`) —
    used by service classes rather than raised as HTTP directly by routers.

    Args:
        code: sets `error_code` on the exception.
    """

    def __init__(self, code: str = "UNPROCESSABLE", message: str = "Request cannot be processed."):
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
        self.error_code = code
