"""Patient intake request/response shapes; see `services/patient_service.py`."""
from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class SexEnum(str, Enum):
    """Patient sex, as recorded at intake."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"


class ConsentData(BaseModel):
    """A patient's consent answers, recorded alongside their intake."""

    consent_given: bool
    consent_storage: bool
    consent_research: bool


class PatientCreateRequest(BaseModel):
    """Request body for creating a patient record."""

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
    """Response body for a patient record. `portal_username`/`portal_password`
    are populated only on creation (the one-time plaintext password isn't
    re-derivable afterward).
    """

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
