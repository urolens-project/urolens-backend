from typing import Optional

from pydantic import BaseModel


class ConfirmResultRequest(BaseModel):
    notes: Optional[str] = None


class ConfirmResultResponse(BaseModel):
    result_id: str
    status: str
    confirmed_at: str
