from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel

ProbabilityLevel = Literal["LOW", "MODERATE", "HIGH"]


class EvidenceMap(BaseModel):
    gout: List[str] = []
    uti: List[str] = []
    tricho: List[str] = []


class SmartDiagnosisAttached(BaseModel):
    output_id: str
    result_id: str
    status: Literal["ATTACHED"]
    gout_score: ProbabilityLevel
    uti_score: ProbabilityLevel
    tricho_score: ProbabilityLevel
    evidence_map: EvidenceMap
    no_significant_indicators: bool
    engine_version: str
    generated_at: str


class SmartDiagnosisUnavailable(BaseModel):
    result_id: str
    status: Literal["FLAGGED_UNAVAILABLE"]


SmartDiagnosisResponse = Union[SmartDiagnosisAttached, SmartDiagnosisUnavailable]


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
