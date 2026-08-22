"""Patient-portal result listing/detail shapes; see `services/patient_result_service.py`."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

# The fixed set of particle types every result's particle_counts is normalised
# against, in display order — see PatientResultService.get_result_detail.
PARTICLE_LABELS: list[str] = [
    "bacteria",
    "crystals",
    "epithelial-cells",
    "erythrocytes",
    "leukocytes",
    "mucus-threads",
    "sperm-cells",
    "trichomonas-vaginalis",
    "urinary-casts",
    "yeast",
]


class ParticleCount(BaseModel):
    """One particle type's detected count, for one entry of `PARTICLE_LABELS`."""

    label: str
    count: int


class PatientResultItem(BaseModel):
    """One result summary, for the patient's result list."""

    result_id: UUID
    test_type: str
    status: str
    released_at: datetime | None


class PatientResultDetailResponse(BaseModel):
    """Response body for a patient's single-result detail view."""

    status: str
    confirmed_at: datetime | None
    confirmation_notes: str | None
    analyzed_by: str | None
    particle_counts: list[ParticleCount]
    particle_classes: list[str]
    smart_diagnosis_unavailable: bool
    test_type: str
    released_at: datetime | None


# Legacy alias kept for the PDF service which builds its own view of the data.
class PatientResultDetail(PatientResultDetailResponse):
    """Alias of `PatientResultDetailResponse`, used by `pdf_service.generate_result_pdf`."""

    pass
