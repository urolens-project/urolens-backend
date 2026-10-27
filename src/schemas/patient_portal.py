"""Patient-portal result listing/detail shapes; see `services/patient_result_service.py`."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer

from .omit import dropNoneKeys

# The fixed set of particle types every result's particle_counts is normalised
# against, in display order — see PatientResultService.get_result_detail.
# Must match the keys AnalysisResult.ai_findings is actually stored under
# (underscore-separated, e.g. AIFindingsSection.tsx's PARTICLE_CLASSES on the
# frontend) — these were previously hyphenated, so rawFindings.get(label, 0)
# silently returned 0 for every one of these five, regardless of the real
# AI-detected count.
PARTICLE_LABELS: list[str] = [
    "bacteria",
    "crystals",
    "epithelial_cells",
    "erythrocytes",
    "leukocytes",
    "mucus_threads",
    "sperm_cells",
    "trichomonas_vaginalis",
    "urinary_casts",
    "yeast",
]


class ParticleCount(BaseModel):
    """One particle type's detected count, for one entry of `PARTICLE_LABELS`."""

    label: str
    count: int
    displayValue: str | None = None
    """Multi-field results only (left out otherwise): how the average per field is shown,
    `"<1"` for a particle seen but averaging under 0.5, never `"0"` for a seen particle."""

    @model_serializer(mode="wrap")
    def _omitUnsetDisplayValue(self, handler: SerializerFunctionWrapHandler):  # noqa: ANN202
        return dropNoneKeys(handler(self), ("displayValue",))


class PatientResultItem(BaseModel):
    """One result summary, for the patient's result list."""

    resultId: UUID
    testType: str
    status: str
    releasedAt: datetime | None


class PatientResultDetailResponse(BaseModel):
    """Response body for a patient's single-result detail view."""

    status: str
    confirmedAt: datetime | None
    confirmationNotes: str | None
    analyzedBy: str | None
    requestingPhysician: str | None
    particleCounts: list[ParticleCount]
    particleClasses: list[str]
    smartDiagnosisUnavailable: bool
    testType: str
    releasedAt: datetime | None
    fieldCount: int | None = None
    """Fields the counts average over (multi-field results only; left out otherwise)."""
    countsLabel: str | None = None
    """`average per field (N fields)`, or `single image` for a result from before multi-field."""

    @model_serializer(mode="wrap")
    def _omitUnsetFieldKeys(self, handler: SerializerFunctionWrapHandler):  # noqa: ANN202
        return dropNoneKeys(handler(self), ("fieldCount", "countsLabel"))


# Legacy alias kept for the PDF service which builds its own view of the data.
class PatientResultDetail(PatientResultDetailResponse):
    """Alias of `PatientResultDetailResponse`, used by `pdf_service.generate_result_pdf`."""

    pass
