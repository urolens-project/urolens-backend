"""Unit tests — multi-field models and status sets. No database."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers, undefer

from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.base import Base
from src.models.field_annotation_edit import EditAction, FieldAnnotationEdit
from src.models.image import Image
from src.models.manual_override import ManualOverride, OverrideKind
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.services.specimen_access import (
    CAPTURE_PHASE_RESULT_STATUSES,
    FINALISED_RESULT_STATUSES,
    MEDTECH_EDITABLE_RESULT_STATUSES,
    MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES,
    SUPERVISOR_EDITABLE_RESULT_STATUSES,
)

# column -> (nullable, has a server default)
RESULT_FIELD_COLUMNS = {
    "field_id": (False, True),
    "result_id": (False, False),
    "specimen_id": (False, False),
    "image_id": (False, False),
    "field_no": (False, False),
    "magnification": (False, True),
    "status": (False, True),
    "ai_findings": (False, True),
    "detections": (False, True),
    "image_width": (False, False),
    "image_height": (False, False),
    "confidence_scores": (False, True),
    "effective_counts": (False, True),
    "smart_diagnosis": (True, False),
    "smart_diagnosis_unavailable": (False, True),
    "model_version": (False, False),
    "discard_reason": (True, False),
    "verified_by": (True, False),
    "verified_at": (True, False),
    "last_opened_by": (True, False),
    "last_opened_at": (True, False),
    "last_corrected_at": (True, False),
    "version": (False, True),
    "created_at": (False, True),
    "updated_at": (False, True),
}


def test_resultFieldsColumnsMatchTheSpec():
    table = Base.metadata.tables["result_fields"]
    assert set(table.columns.keys()) == set(RESULT_FIELD_COLUMNS)
    for name, (nullable, hasDefault) in RESULT_FIELD_COLUMNS.items():
        column = table.columns[name]
        assert column.nullable is nullable, name
        if name != "field_id":
            assert (column.server_default is not None) is hasDefault, name


def test_resultFieldsConstraintsAndIndexes():
    table = Base.metadata.tables["result_fields"]
    names = {c.name for c in table.constraints}
    assert {
        "uq_result_fields_image",
        "uq_result_fields_result_field_no",
        "ck_result_fields_magnification",
        "ck_result_fields_status",
        "ck_result_fields_version",
    } <= names
    indexed = {tuple(c.name for c in i.columns) for i in table.indexes}
    assert ("result_id",) in indexed
    assert ("specimen_id",) in indexed
    assert ("specimen_id", "updated_at") in indexed
    assert table.columns["image_id"].type.__class__.__name__ == "UUID"
    assert table.columns["magnification"].type.length == 3
    assert table.columns["model_version"].type.length == 30


def test_fieldAnnotationEditsColumns():
    table = Base.metadata.tables["field_annotation_edits"]
    assert set(table.columns.keys()) == {
        "edit_id", "field_id", "action", "detection_id", "particle_type",
        "x", "y", "w", "h", "edited_by", "edited_at", "superseded_at",
    }
    assert table.columns["detection_id"].type.length == 64
    assert table.columns["superseded_at"].nullable is True
    current = next(i for i in table.indexes if i.name == "ix_field_annotation_edits_current")
    assert [c.name for c in current.columns] == ["field_id"]
    assert str(current.dialect_options["postgresql"]["where"]) == "superseded_at IS NULL"


def test_manualOverridesGainFieldIdAndKind():
    table = Base.metadata.tables["manual_overrides"]
    assert table.columns["field_id"].nullable is True
    assert table.columns["kind"].nullable is False
    assert table.columns["kind"].server_default.arg == "OVERRIDE"
    assert ManualOverride.fieldId.property.columns[0].index is True


def test_analysisResultsGainTheThreeColumns():
    table = Base.metadata.tables["analysis_results"]
    assert table.columns["field_count"].nullable is False
    assert table.columns["field_summary"].nullable is True
    assert table.columns["is_legacy_single_field"].nullable is False


def test_detectionsAreDeferredSoDefaultLoadsNeverSelectThem():
    plain = str(select(ResultField).compile(dialect=postgresql.dialect()))
    assert "detections" not in plain
    withIt = str(select(ResultField).options(undefer(ResultField.detections)).compile(
        dialect=postgresql.dialect()))
    assert "detections" in withIt


def test_fieldRelationshipsNeverLazyLoadByAccident():
    configure_mappers()
    assert AnalysisResult.fields.property.lazy == "raise"
    assert Image.field.property.lazy == "raise"
    assert ResultField.edits.property.lazy == "raise"
    assert FieldAnnotationEdit.field.property.back_populates == "edits"


def test_enumsHoldTheFixedValues():
    assert [s.value for s in FieldStatus] == ["ANALYZED", "ACCEPTED", "DISCARDED"]
    assert [m.value for m in Magnification] == ["HPF", "LPF"]
    assert [a.value for a in EditAction] == ["REMOVED", "ADDED"]
    assert [k.value for k in OverrideKind] == ["OVERRIDE", "REVERT"]


def test_capturingIsAddedAndNothingElseChanged():
    assert ResultStatus.CAPTURING == "CAPTURING"
    assert {s.value for s in ResultStatus} == {
        "CAPTURING", "PENDING_CONFIRM", "PENDING_SUPERVISOR_APPROVAL", "APPROVED",
        "RELEASED", "RETURNED_FOR_CORRECTION", "CRITICAL_ESCALATED",
        "IMAGE_RETAKE_REQUESTED", "FAILED",
    }


def test_statusSetsAreUnchangedAndCaptureSetIsNew():
    assert CAPTURE_PHASE_RESULT_STATUSES == {ResultStatus.CAPTURING}
    assert MEDTECH_EDITABLE_RESULT_STATUSES == {
        ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION}
    assert FINALISED_RESULT_STATUSES == {ResultStatus.APPROVED, ResultStatus.RELEASED}
    assert SUPERVISOR_EDITABLE_RESULT_STATUSES == {ResultStatus.PENDING_SUPERVISOR_APPROVAL}
    assert MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES == MEDTECH_EDITABLE_RESULT_STATUSES | {
        ResultStatus.IMAGE_RETAKE_REQUESTED, ResultStatus.FAILED}
