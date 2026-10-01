"""Unit tests — schemas/result_review.py's EscalateRequest, ReturnRequest, and SpatialAnnotationItem

Covers the request-boundary validation added when consolidating
VALID_ESCALATION_PATHS into a single Literal-backed source of truth (see
changelog.md's "Duplicate VALID_ESCALATION_PATHS constant" entry): an
invalid escalation_path is now rejected by Pydantic before the request even
reaches ResultReviewService.escalate_result, not just by the service's own
runtime check.

ReturnRequest.reason gets the same treatment (UROLENS-151): a blank or
whitespace-only reason is now rejected by Pydantic before the request
reaches ResultReviewService.return_result.

SpatialAnnotationItem (UROLENS-149) gets the same treatment for particleType:
rejected by Pydantic against the existing PARTICLE_LABELS source of truth
before a request reaches ResultReviewService.saveAnnotation. `w`/`h` are
required box dimensions (bug fix: the frontend's AnnotationCanvas always
sends a bounding box, never a bare point) — missing either is rejected too.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.patient_portal import PARTICLE_LABELS
from src.schemas.result_review import (
    VALID_ESCALATION_PATHS,
    EscalateRequest,
    ReturnRequest,
    SpatialAnnotationItem,
)


def test_validEscalationPathsAccepted() -> None:
    for path in VALID_ESCALATION_PATHS:
        request = EscalateRequest(escalationPath=path, escalationNote=None)
        assert request.escalationPath == path


def test_invalidEscalationPathRejectedAtSchemaLevel() -> None:
    with pytest.raises(ValidationError):
        EscalateRequest(escalationPath="NOT_A_REAL_PATH", escalationNote=None)


def test_validReturnReasonAcceptedAndStripped() -> None:
    request = ReturnRequest(reason="  Blurry image  ")
    assert request.reason == "Blurry image"


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
def test_blankOrWhitespaceOnlyReturnReasonRejectedAtSchemaLevel(reason: str) -> None:
    with pytest.raises(ValidationError):
        ReturnRequest(reason=reason)


def test_missingReturnReasonRejectedAtSchemaLevel() -> None:
    with pytest.raises(ValidationError):
        ReturnRequest()


def test_validParticleTypesAccepted() -> None:
    for particleType in PARTICLE_LABELS:
        item = SpatialAnnotationItem(id="a1", x=10, y=20, w=5, h=5, particleType=particleType)
        assert item.particleType == particleType


def test_invalidParticleTypeRejectedAtSchemaLevel() -> None:
    with pytest.raises(ValidationError):
        SpatialAnnotationItem(id="a1", x=10, y=20, w=5, h=5, particleType="not_a_real_particle")


@pytest.mark.parametrize("missingField", ["w", "h"])
def test_missingBoxDimensionRejectedAtSchemaLevel(missingField: str) -> None:
    fields = {"id": "a1", "x": 10, "y": 20, "w": 5, "h": 5, "particleType": "urinary_casts"}
    del fields[missingField]
    with pytest.raises(ValidationError):
        SpatialAnnotationItem(**fields)
