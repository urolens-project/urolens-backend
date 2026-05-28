from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

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
    label: str
    count: int


class PatientResultItem(BaseModel):
    result_id: UUID
    test_type: str
    status: str
    released_at: datetime | None


class PatientResultDetailResponse(BaseModel):
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
    pass
