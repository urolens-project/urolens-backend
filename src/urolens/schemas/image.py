from __future__ import annotations

import uuid
from typing import Optional

from pydantic import BaseModel


class AnalysisResultResponse(BaseModel):
    id: uuid.UUID
    result_id: uuid.UUID
    specimen_id: uuid.UUID
    image_id: Optional[uuid.UUID] = None
    status: str
    ai_findings: Optional[dict] = None
    flagged_anomalies: Optional[dict] = None
    smart_diagnosis: Optional[dict] = None


class ImageDiscardResponse(BaseModel):
    image_id: str
    status: str
    discarded_at: Optional[str] = None
