"""Unit tests — schemas/result_review.py's EscalateRequest

Covers the request-boundary validation added when consolidating
VALID_ESCALATION_PATHS into a single Literal-backed source of truth (see
changelog.md's "Duplicate VALID_ESCALATION_PATHS constant" entry): an
invalid escalation_path is now rejected by Pydantic before the request even
reaches ResultReviewService.escalate_result, not just by the service's own
runtime check.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.urolens.schemas.result_review import VALID_ESCALATION_PATHS, EscalateRequest


def test_validEscalationPathsAccepted():
    for path in VALID_ESCALATION_PATHS:
        request = EscalateRequest(escalationPath=path, escalationNote=None)
        assert request.escalationPath == path


def test_invalidEscalationPathRejectedAtSchemaLevel():
    with pytest.raises(ValidationError):
        EscalateRequest(escalationPath="NOT_A_REAL_PATH", escalationNote=None)
