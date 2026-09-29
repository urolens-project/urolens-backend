"""Unit tests — schemas/result_review.py's EscalateRequest and SpatialAnnotationItem

Covers the request-boundary validation added when consolidating
VALID_ESCALATION_PATHS into a single Literal-backed source of truth (see
changelog.md's "Duplicate VALID_ESCALATION_PATHS constant" entry): an
invalid escalation_path is now rejected by Pydantic before the request even
reaches ResultReviewService.escalate_result, not just by the service's own
runtime check.

SpatialAnnotationItem (UROLENS-149) gets the same treatment for particleType:
rejected by Pydantic against the existing PARTICLE_LABELS source of truth
before a request reaches ResultReviewService.saveAnnotation.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.patient_portal import PARTICLE_LABELS
from src.schemas.result_review import (
    VALID_ESCALATION_PATHS,
    EscalateRequest,
    SpatialAnnotationItem,
)


def test_validEscalationPathsAccepted() -> None:
    for path in VALID_ESCALATION_PATHS:
        request = EscalateRequest(escalationPath=path, escalationNote=None)
        assert request.escalationPath == path


def test_invalidEscalationPathRejectedAtSchemaLevel() -> None:
    with pytest.raises(ValidationError):
        EscalateRequest(escalationPath="NOT_A_REAL_PATH", escalationNote=None)


def test_validParticleTypesAccepted() -> None:
    for particleType in PARTICLE_LABELS:
        item = SpatialAnnotationItem(id="a1", x=10, y=20, particleType=particleType)
        assert item.particleType == particleType


def test_invalidParticleTypeRejectedAtSchemaLevel() -> None:
    with pytest.raises(ValidationError):
        SpatialAnnotationItem(id="a1", x=10, y=20, particleType="not_a_real_particle")
