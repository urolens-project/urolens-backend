"""Write the multi-field request/response shapes as JSON Schema.

Client teams read `docs/contract/field-shapes.json` before the lanes wire the
changed routes (upload, pending list, full result, sync), which are not in
`/openapi.json` yet. Regenerate after any change to `src/schemas/field.py`:

    python -m scripts.export_field_shapes

`tests/test_field_shapes_export.py` fails if the checked-in file is stale.
"""
import json
import sys
from pathlib import Path

from src.schemas import field

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "contract" / "field-shapes.json"

SHAPES = (
    # routes in src/api/fields.py
    "FieldActionResponse",
    "FieldDetail",
    "FieldListResponse",
    "FieldOverrideRequest",
    "ResultStatusResponse",
    "RevertOverrideRequest",
    "SaveAnnotationsRequest",
    "DiscardFieldRequest",
    "VerificationSummaryResponse",
    # routes the lanes will change; schema-only for now
    "FieldAnalysisResponse",
    "MedtechPendingFieldCounts",
    "FullResultFieldAdditions",
    "SyncResultField",
)


def buildShapes() -> dict:
    """JSON Schema for every shape in `SHAPES`, keyed by model name."""
    return {name: getattr(field, name).model_json_schema() for name in SHAPES}


def render() -> str:
    """The exact text of the checked-in file."""
    return json.dumps(buildShapes(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Write `docs/contract/field-shapes.json`."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
