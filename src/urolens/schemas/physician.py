from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class PhysicianPatientItem(BaseModel):
    patient_id: UUID
    patient_uid: str
    first_name: str
    middle_name: Optional[str] = None
    last_name: str
    date_of_birth: str
    sex: str


class LabRequestCreateRequest(BaseModel):
    patient_id: UUID
    test_type: str
    clinical_notes: Optional[str] = None


class LabRequestCreateResponse(BaseModel):
    request_uid: str
    patient_id: UUID
    physician_name: str
    test_type: str
    status: str
    created_at: str


class PhysicianResultSummary(BaseModel):
    result_id: str
    specimen_id: str
    patient_name: str
    patient_uid: str
    patient_age: Optional[int]
    patient_sex: Optional[str]
    status: str
    confirmed_at: Optional[str]
    created_at: str


class PhysicianResultListResponse(BaseModel):
    items: list[PhysicianResultSummary]
    total: int
    page: int
    page_size: int


class SmartDiagnosisDetail(BaseModel):
    gout_score: str
    gn_score: str
    nephro_score: str
    uti_score: str
    tricho_score: str
    evidence_map: dict[str, Any]
    no_significant_indicators: bool
    engine_version: str


class PhysicianResultDetail(BaseModel):
    result_id: str
    specimen_id: str
    patient_name: str
    patient_uid: str
    patient_age: Optional[int]
    patient_sex: Optional[str]
    medtech_name: Optional[str]
    confirmed_at: Optional[str]
    ai_findings: dict[str, Any]
    flagged_anomalies: dict[str, Any]
    particle_classes: dict[str, Any]
    model_version: str
    smart_diagnosis: Optional[SmartDiagnosisDetail]
    image_url: Optional[str]
    status: str
    annotation_notes: Optional[str]
