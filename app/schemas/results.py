from typing import Optional

from pydantic import BaseModel


class ConfirmResultRequest(BaseModel):
    notes: Optional[str] = None


class ConfirmResultResponse(BaseModel):
    result_id: str
    status: str
    confirmed_at: str


class OverrideParameterRequest(BaseModel):
    parameter_name: str
    original_ai_value: str
    corrected_value: str
    rationale: str


class OverrideParameterResponse(BaseModel):
    override_id: str
    result_id: str
    parameter_name: str
