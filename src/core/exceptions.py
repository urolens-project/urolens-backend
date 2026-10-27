"""Domain-specific `HTTPException` subclasses, each carrying a machine-readable
`error_code` alongside the HTTP status/detail. Routes should raise these
directly rather than a bare `HTTPException`, per the response-envelope
convention (no raw `detail=str(e)` to the client).
"""
from __future__ import annotations

from fastapi import HTTPException, status


class ImageFormatError(HTTPException):
    """422 — an uploaded image isn't an accepted format (JPEG/PNG). `error_code`
    is `"INVALID_IMAGE_FORMAT"`.
    """

    def __init__(self, message: str = "Unsupported image format. Accepted: JPEG, PNG."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.errorCode = "INVALID_IMAGE_FORMAT"


class ImageResolutionError(HTTPException):
    """422 — an uploaded image is below the minimum 640×480 resolution.
    `error_code` is `"INVALID_IMAGE_RESOLUTION"`.
    """

    def __init__(self, message: str = "Image resolution is below the required 640×480 minimum."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.errorCode = "INVALID_IMAGE_RESOLUTION"


class ImageTooLargeError(HTTPException):
    """413 — an uploaded image is over the size cap.

    `errorCode` is `"IMAGE_TOO_LARGE"`.
    """

    def __init__(self, message: str = "Image must be 10 MB or smaller.") -> None:
        """Build the 413 with `message` as the client-facing detail."""
        super().__init__(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=message,
        )
        self.errorCode = "IMAGE_TOO_LARGE"


class StorageError(HTTPException):
    """503 — storing/retrieving a file (e.g. Supabase storage) failed.
    `error_code` is `"STORAGE_ERROR"`.
    """

    def __init__(self, message: str = "Failed to store image. Please try again."):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=message,
        )
        self.errorCode = "STORAGE_ERROR"


class AIAnalysisError(HTTPException):
    """503 — AI analysis of an uploaded image failed, so nothing was saved.

    `errorCode` is `"AI_ANALYSIS_FAILED"`. The client keeps the image and lets
    the MedTech try again (UROLENS-230).
    """

    def __init__(
        self, message: str = "The image couldn't be analyzed. Please try uploading it again."
    ) -> None:
        """Build the 503 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
        self.errorCode = "AI_ANALYSIS_FAILED"


class NotFoundError(HTTPException):
    """404 — the requested resource doesn't exist. `error_code` is `"NOT_FOUND"`."""

    def __init__(self, message: str = "Resource not found."):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=message,
        )
        self.errorCode = "NOT_FOUND"


class ConflictError(HTTPException):
    """409 — the resource exists but is in a state that conflicts with the
    request. `error_code` is `"CONFLICT"`.
    """

    def __init__(self, message: str = "Resource state conflict."):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )
        self.errorCode = "CONFLICT"


class SpecimenNotFoundError(NotFoundError):
    """404 — a specimen lookup by ID found nothing. `error_code` is
    `"SPECIMEN_NOT_FOUND"`.

    Args:
        specimen_id: included in the default message when given; the message
            falls back to a generic "not found" if omitted.
    """

    def __init__(self, specimenId: str | None = None):
        msg = f"Specimen {specimenId} not found." if specimenId else "Specimen not found."
        super().__init__(message=msg)
        self.errorCode = "SPECIMEN_NOT_FOUND"


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
        self.errorCode = code


class ForbiddenException(HTTPException):
    """403, with a caller-supplied `errorCode` (default `"FORBIDDEN"`).

    The caller is authenticated and has the right role, but no access to this
    particular record (e.g. `SPECIMEN_NOT_ASSIGNED`).
    """

    def __init__(self, code: str = "FORBIDDEN", message: str = "Access to this resource is not allowed.") -> None:
        """Build the 403.

        Args:
            code: sets `errorCode` on the exception.
            message: the client-facing detail.
        """
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=message)
        self.errorCode = code


class TooManyRequestsException(HTTPException):
    """429 — too many login attempts; `errorCode` is `"TOO_MANY_LOGIN_ATTEMPTS"`.

    Carries a `Retry-After` header (seconds), which the global error handler
    passes through to the client.
    """

    def __init__(self, retryAfterSeconds: int) -> None:
        """Build the 429 telling the client to wait `retryAfterSeconds`."""
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {retryAfterSeconds} seconds.",
            headers={"Retry-After": str(retryAfterSeconds)},
        )
        self.errorCode = "TOO_MANY_LOGIN_ATTEMPTS"


class ConflictException(HTTPException):
    """409, with a caller-supplied `error_code` (default `"CONFLICT"`) — used
    by service classes rather than raised as HTTP directly by routers.

    Args:
        code: sets `error_code` on the exception; distinct from `ConflictError`,
            which hardcodes it.
    """

    def __init__(self, code: str = "CONFLICT", message: str = "Resource state conflict."):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=message)
        self.errorCode = code


class UnprocessableException(HTTPException):
    """422, with a caller-supplied `error_code` (default `"UNPROCESSABLE"`) —
    used by service classes rather than raised as HTTP directly by routers.

    Args:
        code: sets `error_code` on the exception.
    """

    def __init__(self, code: str = "UNPROCESSABLE", message: str = "Request cannot be processed."):
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
        self.errorCode = code


# ── Multi-field workflow ─────────────────────────────────────────────────────
# Backend-owned codes the mobile and web apps tell apart. The error envelope
# carries only `code` and `message`, so any numbers go in the message.


class ImageNotMicroscopyError(HTTPException):
    """422 — the engine's input gate says this is not a microscope image.
    `errorCode` is `"IMAGE_NOT_MICROSCOPY"`; nothing is saved."""

    def __init__(
        self, message: str = "This does not look like a microscope image. Retake it."
    ) -> None:
        """Build the 422 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
        self.errorCode = "IMAGE_NOT_MICROSCOPY"


class ImageExposureError(HTTPException):
    """422 — the engine's input gate says the image is badly exposed.
    `errorCode` is `"IMAGE_EXPOSURE"`; nothing is saved."""

    def __init__(
        self,
        message: str = "Check the light and fill the screen with the eyepiece circle, then retake.",
    ) -> None:
        """Build the 422 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
        self.errorCode = "IMAGE_EXPOSURE"


class AppUpdateRequiredError(HTTPException):
    """410 — a retired route called by an app version that predates multi-field.
    `errorCode` is `"APP_UPDATE_REQUIRED"`."""

    def __init__(self, message: str = "Update UroLens to continue") -> None:
        """Build the 410 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_410_GONE, detail=message)
        self.errorCode = "APP_UPDATE_REQUIRED"


class FieldNotOpenedError(ConflictException):
    """409 `FIELD_NOT_OPENED` — verify was called before the same user opened the
    field since its last correction."""

    def __init__(self, message: str = "Open the field before marking it as checked.") -> None:
        """Build the 409 with `message` as the client-facing detail."""
        super().__init__(code="FIELD_NOT_OPENED", message=message)


class FieldVersionConflictError(ConflictException):
    """409 `FIELD_VERSION_CONFLICT` — the field changed since the client loaded it;
    nothing was written."""

    def __init__(
        self, message: str = "This field was changed by someone else. Reload it and try again."
    ) -> None:
        """Build the 409 with `message` as the client-facing detail."""
        super().__init__(code="FIELD_VERSION_CONFLICT", message=message)


class OverrideInForceError(ConflictException):
    """409 `OVERRIDE_IN_FORCE` — box edits to a class that has a numeric override in
    force; revert the override first."""

    def __init__(
        self,
        message: str = "This class has a manual count in force. Remove the override to edit its boxes.",
    ) -> None:
        """Build the 409 with `message` as the client-facing detail."""
        super().__init__(code="OVERRIDE_IN_FORCE", message=message)


class FieldsUndecidedError(ConflictException):
    """409 `FIELDS_UNDECIDED` — fields are still waiting for Proceed or Discard."""

    def __init__(self, count: int) -> None:
        """Build the 409; `count` is the number of fields not reviewed."""
        noun = "field" if count == 1 else "fields"
        super().__init__(code="FIELDS_UNDECIDED", message=f"{count} {noun} not reviewed.")


class FieldsBelowMinimumError(ConflictException):
    """409 `FIELDS_BELOW_MINIMUM` — fewer accepted fields than the minimum."""

    def __init__(self, accepted: int, minimum: int) -> None:
        """Build the 409; the message names both numbers."""
        super().__init__(
            code="FIELDS_BELOW_MINIMUM",
            message=f"{accepted} of {minimum} required fields accepted.",
        )


class FieldsNotVerifiedError(ConflictException):
    """409 `FIELDS_NOT_VERIFIED` — an accepted field has not been checked."""

    def __init__(self, message: str = "Every accepted field must be checked first.") -> None:
        """Build the 409 with `message` as the client-facing detail."""
        super().__init__(code="FIELDS_NOT_VERIFIED", message=message)


class FeatureDisabledError(HTTPException):
    """404 `FEATURE_DISABLED` — a multi-field route called while
    `fieldWorkflowEnabled` is off."""

    def __init__(self, message: str = "This feature is not available.") -> None:
        """Build the 404 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=message)
        self.errorCode = "FEATURE_DISABLED"


class NotImplementedYetError(HTTPException):
    """501 `NOT_IMPLEMENTED` — a multi-field route whose handler is not written yet."""

    def __init__(self, message: str = "This endpoint is not implemented yet.") -> None:
        """Build the 501 with `message` as the client-facing detail."""
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=message)
        self.errorCode = "NOT_IMPLEMENTED"
