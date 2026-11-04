"""Unit tests — `GET /sync/pull` as the phone reads it (multi-field flag on).

The phone builds its whole local database from this payload (`sync.mapper.ts` and
`pullChanges.ts` in urolens-mobile), so a renamed or missing key breaks it silently:
the mapper reads `undefined`, stores null, and the server wins on the next pull (as
`field_count` did in mobile PR #55's first review, 3.1).

- Every key the phone reads is in each table's rows, with `fieldId` as the identity
  of a field row and `id` everywhere else; `minFields` is beside `timestamp`, and
  result rows carry `field_summary`.
- The whole response JSON matches `tests/fixtures/sync_pull_golden.json`. After an
  intended change to the payload, regenerate it with
  `UPDATE_SYNC_GOLDEN=1 pytest tests/test_sync_contract.py`, check the diff, and tell
  the mobile developer.
DB is mocked.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import settings
from src.models.manual_override import ManualOverride
from src.models.queue_assignment import QueueAssignment
from src.models.result_field import FieldStatus, Magnification, ResultField
from src.schemas.sync import SyncPullResponse
from src.services import sync_service
from tests.conftest import SYNC_MEDTECH_ID, makeSyncDb, syncResult, syncSpecimen

GOLDEN = Path(__file__).parent / "fixtures" / "sync_pull_golden.json"

CAPTURING_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000c001")
CAPTURING_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c002")
CONFIRMED_SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-00000000c003")
CONFIRMED_RESULT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c004")
ASSIGNMENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000c005")
OVERRIDE_ID = uuid.UUID("00000000-0000-0000-0000-00000000c006")
FIELD_ID = uuid.UUID("00000000-0000-0000-0000-00000000c007")
IMAGE_ID = uuid.UUID("00000000-0000-0000-0000-00000000c008")
AT = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)

# Keys the phone's sync mapper reads, per table (urolens-mobile
# src/db/mappers/sync.mapper.ts, PR #55 at baa3104). `field_summary` and `minFields`
# are read once the Reports and minimum follow-ups land (2.3, 2.4 of the PR #55 list).
PHONE_READS: dict[str, set[str]] = {
    "specimens": {
        "id", "sample_uid", "patient_uid", "test_type", "status", "priority_level", "received_at",
        "assigned_at", "completed_at", "medtech_id", "rejection_reason", "rejection_note", "rejected_at",
    },
    "queueAssignments": {"id", "specimen_id", "medtech_id", "assigned_at", "status"},
    "analysisResults": {
        "id", "specimen_id", "ai_findings", "flagged_anomalies", "particle_classes", "smart_diagnosis",
        "smart_diagnosis_unavailable", "status", "image_id", "confirmed_at", "confirmed_by",
        "approved_at", "released_at", "return_reason", "field_summary",
    },
    "manualOverrides": {
        "id", "result_id", "parameter_name", "original_ai_value", "corrected_value", "rationale",
        "medtech_id", "overridden_at",
    },
    "resultFields": {
        "fieldId", "resultId", "specimenId", "imageId", "fieldNo", "magnification", "status",
        "aiFindings", "effectiveCounts", "confidenceScores", "imageWidth", "imageHeight",
        "smartDiagnosisPreview", "smartDiagnosisUnavailable", "version",
    },
}

STORED_SUMMARY = {
    "fieldCount": 10,
    "perClass": {
        "casts": {"mean": 0.1, "min": 0, "max": 1, "total": 1, "fieldsSeen": 1, "aiMean": 0.1},
        "erythrocytes": {"mean": 2.4, "min": 1, "max": 5, "total": 24, "fieldsSeen": 10, "aiMean": 2.6},
    },
    "focalFindings": [{"particleType": "erythrocytes", "fieldNos": [7]}],
}


def _fullSyncDb() -> AsyncMock:
    """One sample still capturing (with a field) and one confirmed (with its summary,
    an approval and an override)."""
    capturing = syncSpecimen(specimenId=CAPTURING_SPECIMEN_ID, sampleUid="SMP-20261102-00001")
    capturingResult = syncResult(capturing, resultId=CAPTURING_RESULT_ID, status="CAPTURING")
    confirmed = syncSpecimen(
        specimenId=CONFIRMED_SPECIMEN_ID, sampleUid="SMP-20261102-00002", status="COMPLETED",
        completedAt=AT,
    )
    confirmedResult = syncResult(
        confirmed, resultId=CONFIRMED_RESULT_ID, status="APPROVED", fieldSummary=STORED_SUMMARY,
        particleClasses={"casts": 1, "erythrocytes": 2}, confirmedAt=AT, confirmedBy=SYNC_MEDTECH_ID,
    )
    assignment = QueueAssignment(
        assignmentId=ASSIGNMENT_ID, specimenId=CAPTURING_SPECIMEN_ID, medtechId=SYNC_MEDTECH_ID,
        assignedAt=AT, status="ACTIVE",
    )
    override = ManualOverride(
        overrideId=OVERRIDE_ID, resultId=CONFIRMED_RESULT_ID, parameterName="erythrocytes",
        originalAiValue="3", correctedValue="2", rationale="double-counted", medtechId=SYNC_MEDTECH_ID,
        overriddenAt=AT,
    )
    field = ResultField(
        fieldId=FIELD_ID, resultId=CAPTURING_RESULT_ID, specimenId=CAPTURING_SPECIMEN_ID, imageId=IMAGE_ID,
        fieldNo=1, magnification=Magnification.HPF, status=FieldStatus.ANALYZED,
        aiFindings={"erythrocytes": 3}, effectiveCounts={"erythrocytes": 3},
        confidenceScores={"erythrocytes": 0.81}, imageWidth=800, imageHeight=600,
        smartDiagnosis={"gout": {"level": "LOW"}}, smartDiagnosisUnavailable=False,
        modelVersion="v0.2.0", version=1, createdAt=AT, updatedAt=AT,
    )
    return makeSyncDb(
        rows=[(capturing, capturingResult), (confirmed, confirmedResult)],
        assignments=[assignment],
        approvals=[(CONFIRMED_RESULT_ID, AT)],
        overrides=[override],
        fields=[field],
    )


async def _fullSyncJson(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    monkeypatch.setattr(settings, "minFieldsPerSpecimen", 10)
    with patch.object(sync_service, "AuditLogger", return_value=MagicMock(record=AsyncMock())):
        payload = await sync_service.pull(_fullSyncDb(), str(SYNC_MEDTECH_ID), None)
    body = SyncPullResponse.model_validate(payload).model_dump(mode="json")
    body["timestamp"] = "<server time>"
    return body


@pytest.mark.asyncio
async def test_everyKeyThePhoneReadsIsInTheFullSync(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _fullSyncJson(monkeypatch)

    assert {"timestamp", "minFields", "changes"} <= set(body)
    assert body["minFields"] == 10
    for table, keys in PHONE_READS.items():
        rows = body["changes"][table]["created"]
        assert rows, table
        for row in rows:
            missing = keys - set(row)
            assert not missing, f"{table} rows lack {sorted(missing)}"


@pytest.mark.asyncio
async def test_fieldRowsNeverCarryTheBoxes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _fullSyncJson(monkeypatch)

    for row in body["changes"]["resultFields"]["created"]:
        assert "detections" not in row


@pytest.mark.asyncio
async def test_theFullSyncMatchesTheGolden(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _fullSyncJson(monkeypatch)
    text = json.dumps(body, indent=2, sort_keys=True) + "\n"

    if os.environ.get("UPDATE_SYNC_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8")
