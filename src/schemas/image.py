"""Image upload/discard response shapes; see `api/image.py`."""
from __future__ import annotations

import uuid

from pydantic import BaseModel


class AnalysisResultResponse(BaseModel):
    """Response body for a successful image upload — the specimen's
    AnalysisResult, including AI findings if inference succeeded.
    """

    id: uuid.UUID
    resultId: uuid.UUID
    specimenId: uuid.UUID
    imageId: uuid.UUID | None = None
    status: str
    aiFindings: dict | None = None
    flaggedAnomalies: dict | None = None
    smartDiagnosis: dict | None = None


class ImageDiscardResponse(BaseModel):
    """Response body for a successful image discard (retake flow)."""

    imageId: str
    status: str
    discardedAt: str | None = None
