from __future__ import annotations

from fastapi import HTTPException, status


class ImageFormatError(HTTPException):
    def __init__(self, message: str = "Unsupported image format. Accepted: JPEG, PNG."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.error_code = "INVALID_IMAGE_FORMAT"


class ImageResolutionError(HTTPException):
    def __init__(self, message: str = "Image resolution is below the required 640×480 minimum."):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=message,
        )
        self.error_code = "INVALID_IMAGE_RESOLUTION"


class StorageError(HTTPException):
    def __init__(self, message: str = "Failed to store image. Please try again."):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=message,
        )
        self.error_code = "STORAGE_ERROR"


class NotFoundError(HTTPException):
    def __init__(self, message: str = "Resource not found."):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=message,
        )
        self.error_code = "NOT_FOUND"


class ConflictError(HTTPException):
    def __init__(self, message: str = "Resource state conflict."):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=message,
        )
        self.error_code = "CONFLICT"


class SpecimenNotFoundError(NotFoundError):
    def __init__(self, specimen_id: str | None = None):
        msg = f"Specimen {specimen_id} not found." if specimen_id else "Specimen not found."
        super().__init__(message=msg)
        self.error_code = "SPECIMEN_NOT_FOUND"
