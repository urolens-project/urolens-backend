from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class ConsentData(BaseModel):
    consent_given: bool
    consent_storage: bool
    consent_research: bool


class PatientCreateRequest(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: date
    contact_no: str | None = None
    address: str | None = None
    consent: ConsentData


class PatientResponse(BaseModel):
    patient_id: UUID
    patient_uid: str
    first_name: str
    last_name: str
    date_of_birth: str
    contact_no: str | None = None
    address: str | None = None
    is_walkin: bool
    record_flag: str | None = None
    created_at: datetime
