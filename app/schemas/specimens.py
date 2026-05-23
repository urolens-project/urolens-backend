from typing import Optional

from pydantic import BaseModel


class RejectSpecimenRequest(BaseModel):
    reason_code: str
    free_text_note: Optional[str] = None


class RejectSpecimenResponse(BaseModel):
    specimen_id: str
    status: str
    rejected_at: str
