"""Patient intake request/response shapes; see `services/patient_service.py`."""
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, field_validator

_CONSENT_REQUIRED_MESSAGE = "This consent must be confirmed before registration can proceed."


class SexEnum(StrEnum):
    """Patient sex, as recorded at intake."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"


class ConsentData(BaseModel):
    """A patient's consent answers, recorded alongside their intake.

    Processing (`consentGiven`) and storage consent are required for
    registration — each is validated independently so a request with both
    unconfirmed surfaces one inline error per item, not just the first.
    Research consent is optional: consent that is a condition of receiving
    care isn't freely given (RA 10173), and research isn't needed to run the
    lab test (UROLENS-222).
    """

    consentGiven: bool
    consentStorage: bool
    consentResearch: bool = False
    """Optional; defaults to `False` (not consented) when omitted."""

    @field_validator("consentGiven")
    @classmethod
    def consentGivenMustBeTrue(cls, v: bool) -> bool:
        if not v:
            raise ValueError(_CONSENT_REQUIRED_MESSAGE)
        return v

    @field_validator("consentStorage")
    @classmethod
    def consentStorageMustBeTrue(cls, v: bool) -> bool:
        if not v:
            raise ValueError(_CONSENT_REQUIRED_MESSAGE)
        return v


class PatientCreateRequest(BaseModel):
    """Request body for creating a patient record."""

    firstName: str
    middleName: str | None = None
    lastName: str
    dateOfBirth: date
    sex: SexEnum
    contactNo: str | None = None
    address: str | None = None
    clinicalHistory: str | None = None
    isWalkin: bool = False
    consent: ConsentData

    @field_validator("firstName", "lastName")
    @classmethod
    def nameNotBlank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("dateOfBirth")
    @classmethod
    def dateOfBirthMustBePast(cls, v: date) -> date:
        if v >= date.today():
            raise ValueError("dateOfBirth must be a date in the past")
        return v


class PatientSearchItem(BaseModel):
    """One patient search result for `GET /api/v1/patients?q=` — deliberately
    minimal (RA 10173 data minimization): no name, DOB, or contact info. The
    caller already has `q` (what they typed) and only needs an identifier to
    select a patient and act on it (e.g. as `patientId` on a lab request);
    see `PatientService.search_patients`.
    """

    patientId: UUID
    patientUid: str


class PatientResponse(BaseModel):
    """Response body for a patient record. `portal_username`/`portal_password`
    are populated only on creation (the one-time plaintext password isn't
    re-derivable afterward).
    """

    patientId: UUID
    patientUid: str
    firstName: str
    middleName: str | None = None
    lastName: str
    dateOfBirth: str
    sex: str
    contactNo: str | None = None
    address: str | None = None
    clinicalHistory: str | None = None
    isWalkin: bool
    recordFlag: str | None = None
    createdAt: datetime
    userId: UUID | None = None
    portalUsername: str | None = None
    portalPassword: str | None = None
