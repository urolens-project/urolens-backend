"""Image upload/discard response shapes; see `api/image.py`."""
from __future__ import annotations

import uuid

from pydantic import BaseModel


class AnalysisResultResponse(BaseModel):
    """Response body for a successful image upload — the specimen's
    AnalysisResult, including AI findings if inference succeeded.
    """

    id: uuid.UUID
    result_id: uuid.UUID
    specimen_id: uuid.UUID
    image_id: uuid.UUID | None = None
    status: str
    ai_findings: dict | None = None
    flagged_anomalies: dict | None = None
    smart_diagnosis: dict | None = None


class ImageDiscardResponse(BaseModel):
    """Response body for a successful image discard (retake flow)."""

    image_id: str
    status: str
    discarded_at: str | None = None
