"""Unit tests — pdf_service.generateResultPdf's Interpretation section (UROLENS-156).

Confirms the PDF's "Pending review" fallback (pdf_service.py:~168) is no
longer the permanent result for every PDF now that
ResultConfirmationService actually writes
`AnalysisResult.interpretation` -> `PatientResultDetail.confirmationNotes`
flows through unchanged; the fallback itself is NOT being removed, just no
longer the only possible outcome.

No PDF-text-extraction library is a project dependency, so rather than parse
the rendered PDF bytes, these patch reportlab's `Paragraph` to capture what
text the Interpretation section actually builds with, and no-op
`SimpleDocTemplate.build` so `generateResultPdf` can run to completion
without needing a real renderable document.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from src.schemas.patient_portal import (
    PARTICLE_LABELS,
    ParticleCount,
    PatientResultDetail,
)
from src.services import pdf_service


def _makeDetail(confirmationNotes: str | None) -> PatientResultDetail:
    return PatientResultDetail(
        status="RELEASED",
        confirmedAt=None,
        confirmationNotes=confirmationNotes,
        analyzedBy=None,
        particleCounts=[ParticleCount(label=label, count=0) for label in PARTICLE_LABELS],
        particleClasses=[],
        smartDiagnosisUnavailable=True,
        testType="Urinalysis",
        releasedAt=None,
    )


def _capturedInterpretationText(confirmationNotes: str | None) -> str:
    captured: list[str] = []

    def _fakeParagraph(text, *args, **kwargs):
        captured.append(text)
        return MagicMock()

    with patch("reportlab.platypus.Paragraph", side_effect=_fakeParagraph), \
         patch("reportlab.platypus.SimpleDocTemplate.build", return_value=None):
        pdf_service.generateResultPdf(_makeDetail(confirmationNotes), "Jane Doe", uuid.uuid4())

    headingIndex = captured.index("Interpretation")
    return captured[headingIndex + 1]


def test_generateResultPdfPrintsTheMedtechsNotesWhenPresent() -> None:
    text = _capturedInterpretationText("No significant abnormalities detected.")
    assert text == "No significant abnormalities detected."
    assert text != "Pending review"


def test_generateResultPdfFallsBackToPendingReviewWhenNotesAreNone() -> None:
    """The fallback stays in place for a result a MedTech genuinely left
    without notes — this is unchanged behavior, not something being removed.
    """
    text = _capturedInterpretationText(None)
    assert text == "Pending review"
