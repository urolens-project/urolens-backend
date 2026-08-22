"""Image upload/discard response shapes; see `api/image.py`."""
from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel


class AnalysisResultResponse(BaseModel):
    """Response body for a successful image upload — the specimen's
    AnalysisResult, including AI findings if inference succeeded."""

    id: uuid.UUID
    result_id: uuid.UUID
    specimen_id: uuid.UUID
    image_id: Optional[uuid.UUID] = None
    status: str
    ai_findings: Optional[dict] = None
    flagged_anomalies: Optional[dict] = None
    smart_diagnosis: Optional[dict] = None


class ImageDiscardResponse(BaseModel):
    """Response body for a successful image discard (retake flow)."""

    image_id: str
    status: str
    discarded_at: Optional[str] = None
