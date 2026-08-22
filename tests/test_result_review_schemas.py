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


def test_valid_escalation_paths_accepted():
    for path in VALID_ESCALATION_PATHS:
        request = EscalateRequest(escalation_path=path, escalation_note=None)
        assert request.escalation_path == path


def test_invalid_escalation_path_rejected_at_schema_level():
    with pytest.raises(ValidationError):
        EscalateRequest(escalation_path="NOT_A_REAL_PATH", escalation_note=None)
