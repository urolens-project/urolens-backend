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
    smartDiagnosisUnavailable: bool = False
    """True when the Smart Diagnosis engine failed for this image, so the app
    can show the "not available" notice straight away (UROLENS-230)."""


class ImageDiscardResponse(BaseModel):
    """Response body for a successful image discard (retake flow)."""

    imageId: str
    status: str
    discardedAt: str | None = None
