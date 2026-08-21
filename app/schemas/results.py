from typing import Any, Dict, List, Literal, Union
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
    gn_score: ProbabilityLevel
    nephro_score: ProbabilityLevel
    evidence_map: Dict[str, Any]
    no_significant_indicators: bool
    engine_version: str
    generated_at: str


class SmartDiagnosisUnavailable(BaseModel):
    result_id: str
    status: Literal["FLAGGED_UNAVAILABLE"]


SmartDiagnosisResponse = Union[SmartDiagnosisAttached, SmartDiagnosisUnavailable]
