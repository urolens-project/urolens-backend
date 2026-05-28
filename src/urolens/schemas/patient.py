from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class SexEnum(str, Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"


class ConsentData(BaseModel):
    consent_given: bool
    consent_storage: bool
    consent_research: bool


class PatientCreateRequest(BaseModel):
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: date
    sex: SexEnum
    contact_no: str | None = None
    address: str | None = None
    clinical_history: str | None = None
    is_walkin: bool = False
    consent: ConsentData


class PatientResponse(BaseModel):
    patient_id: UUID
    patient_uid: str
    first_name: str
    middle_name: str | None = None
    last_name: str
    date_of_birth: str
    sex: str
    contact_no: str | None = None
    address: str | None = None
    clinical_history: str | None = None
    is_walkin: bool
    record_flag: str | None = None
    created_at: datetime
    user_id: UUID | None = None
    portal_username: str | None = None
    portal_password: str | None = None
