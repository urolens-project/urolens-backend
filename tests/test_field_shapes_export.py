"""Unit tests — `docs/contract/field-shapes.json` matches `src/schemas/field.py`."""
from __future__ import annotations

from scripts.export_field_shapes import OUTPUT_PATH, SHAPES, buildShapes, render


def test_checkedInFileIsCurrent():
    assert OUTPUT_PATH.read_text(encoding="utf-8") == render(), (
        "docs/contract/field-shapes.json is stale. Run: python -m scripts.export_field_shapes"
    )


def test_everyListedShapeIsExported():
    assert set(buildShapes()) == set(SHAPES)


def test_changedRouteShapesNeverCarryDetectionsExceptUpload():
    shapes = buildShapes()
    for name in ("MedtechPendingFieldCounts", "FullResultFieldAdditions", "SyncResultField"):
        assert "detections" not in shapes[name].get("properties", {}), name
    assert "detections" in shapes["FieldAnalysisResponse"]["properties"]
