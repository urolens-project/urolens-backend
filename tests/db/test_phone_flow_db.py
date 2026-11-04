"""Real-database tests — the MedTech phone's flow on a copy of live's schema.

The real services run on a real session; only the AI engine and storage are stubbed
(see conftest.py for building the copy and the safety checks).

- Capture: 11 fields; a retried upload with the same capture id gets the first field
  back (the unique index on specimen and capture id is real here); the specimen
  starts processing.
- Sync: a full pull carries the fields, `minFields` and a null `field_summary`; after
  Proceed and Discard, a delta pull carries exactly the decided fields (their
  `updated_at` really moved).
- Submit is refused while a field waits for a decision, and the refusal names that
  field (`details.undecidedFields`) so a phone missing it can fetch it.
- Submit moves the result to `PENDING_CONFIRM`; after the website checks every field
  and confirms, a delta pull shows `PENDING_SUPERVISOR_APPROVAL` and the averages.
- Supervisor return → website Capture More Fields → a CAPTURE_REOPENED notification
  is stored, sync shows `CAPTURING` with the return reason, and a submit answers
  `RETURNED_FOR_CORRECTION`.
- Rejecting an assigned specimen from the phone stores the reason on the specimen
  and its audit row (the receiving desk's `specimen_rejections` is a website path).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.config import settings
from src.models.analysis_result import ResultStatus
from src.services import specimen_service, sync_service
from src.services.field_correction_service import FieldCorrectionService
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_review_service import ResultReviewService
from src.services.smart_diagnosis_service import SmartDiagnosisService
from tests.db.conftest import (
    capture,
    fieldService,
    requiresLiveCopy,
    seedAssignedSpecimen,
)

pytestmark = [requiresLiveCopy, pytest.mark.usefixtures("stubbedEngineAndStorage", "fieldsOn")]

RETURN_REASON = "Recount the erythrocytes in field 3"


@pytest.fixture
def fieldsOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)


async def _pull(db: AsyncSession, ids: SimpleNamespace, since: datetime | None = None) -> dict[str, Any]:
    """This specimen's rows from a sync pull, and the pull's own keys."""
    payload = await sync_service.pull(db, str(ids.medtechId), since)
    table = "updated" if since else "created"
    changes = payload["changes"]
    return {
        "timestamp": datetime.fromisoformat(payload["timestamp"]),
        "minFields": payload.get("minFields"),
        "results": [r for r in changes["analysisResults"][table] if r["specimen_id"] == str(ids.specimenId)],
        "fields": [f for f in changes["resultFields"][table] if f["specimenId"] == str(ids.specimenId)],
    }


async def _submittedTenFields(db: AsyncSession) -> tuple[SimpleNamespace, list]:
    ids = await seedAssignedSpecimen(db)
    fields = [await capture(db, ids) for _ in range(10)]
    service = fieldService(db)
    for field in fields:
        await service.acceptField(field.fieldId, ids.medtechId, None)
    submitted = await service.submitFields(fields[0].resultId, ids.medtechId, None)
    assert submitted.status == ResultStatus.PENDING_CONFIRM
    return ids, fields


async def _confirmedOnTheWebsite(db: AsyncSession, ids: SimpleNamespace, fields: list) -> None:
    claims = {"user_id": str(ids.medtechId), "role": "MEDTECH"}
    service, corrections = fieldService(db), FieldCorrectionService(db=db, auditLogger=AuditLogger())
    for field in fields:
        await service.getField(field.fieldId, claims, None)  # the website open that verify needs
        await corrections.markVerified(field.fieldId, ids.medtechId, None)
    notifications = NotificationService(db)
    confirmation = ResultConfirmationService(
        db=db, auditLogger=AuditLogger(),
        _smartDiagnosisService=SmartDiagnosisService(auditLogger=AuditLogger(), _notifService=notifications),
        _notifService=notifications,
    )
    response = await confirmation.confirmResult(fields[0].resultId, ids.medtechId, None)
    assert response.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL


@pytest.mark.asyncio
async def test_aRefusedSubmitNamesTheUndecidedFields(db: AsyncSession) -> None:
    ids = await seedAssignedSpecimen(db)
    fields = [await capture(db, ids) for _ in range(11)]
    service = fieldService(db)
    for field in fields[:4] + fields[5:10]:
        await service.acceptField(field.fieldId, ids.medtechId, None)

    with pytest.raises(HTTPException) as excInfo:
        await service.submitFields(fields[0].resultId, ids.medtechId, None)

    assert excInfo.value.errorCode == "FIELDS_UNDECIDED"
    assert excInfo.value.detail == "2 fields not reviewed."
    assert excInfo.value.errorDetails == {"undecidedFields": [
        {"fieldId": str(fields[4].fieldId), "fieldNo": fields[4].fieldNo},
        {"fieldId": str(fields[10].fieldId), "fieldNo": fields[10].fieldNo},
    ]}


@pytest.mark.asyncio
async def test_capturingDecidingAndSubmittingRunOnLivesSchema(db: AsyncSession) -> None:
    ids = await seedAssignedSpecimen(db)
    first = await capture(db, ids, captureId="retry-test-0001")
    again = await capture(db, ids, captureId="retry-test-0001")
    fields = [first] + [await capture(db, ids) for _ in range(10)]

    assert (again.fieldId, again.fieldNo) == (first.fieldId, first.fieldNo)
    stored = await db.scalar(text("SELECT count(*) FROM result_fields WHERE specimen_id = :s"), {"s": ids.specimenId})
    assert stored == 11
    assert [f.fieldNo for f in fields] == list(range(1, 12))
    assert await db.scalar(text("SELECT status FROM specimens WHERE specimen_id = :s"), {"s": ids.specimenId}) == (
        "PROCESSING"
    )

    full = await _pull(db, ids)
    assert full["minFields"] == settings.minFieldsPerSpecimen
    [result] = full["results"]
    assert (result["status"], result["field_summary"]) == ("CAPTURING", None)
    assert sorted(f["status"] for f in full["fields"]) == ["ANALYZED"] * 11

    service = fieldService(db)
    for field in fields[:10]:
        await service.acceptField(field.fieldId, ids.medtechId, None)
    await service.discardField(fields[10].fieldId, ids.medtechId, None, None)

    delta = await _pull(db, ids, since=full["timestamp"])
    assert {f["fieldId"]: f["status"] for f in delta["fields"]} == {
        **{str(f.fieldId): "ACCEPTED" for f in fields[:10]},
        str(fields[10].fieldId): "DISCARDED",
    }

    submitted = await service.submitFields(first.resultId, ids.medtechId, None)
    assert submitted.status == ResultStatus.PENDING_CONFIRM


@pytest.mark.asyncio
async def test_theWebsitesConfirmReachesThePhoneWithTheAverages(db: AsyncSession) -> None:
    ids, fields = await _submittedTenFields(db)
    before = await _pull(db, ids)

    await _confirmedOnTheWebsite(db, ids, fields)

    delta = await _pull(db, ids, since=before["timestamp"])
    [result] = delta["results"]
    assert result["status"] == "PENDING_SUPERVISOR_APPROVAL"
    summary = result["field_summary"]
    assert summary["fieldCount"] == 10
    assert summary["perClass"]["erythrocytes"]["mean"] == 2.0
    assert summary["perClass"]["erythrocytes"]["displayValue"] == "2"
    assert result["particle_classes"] == {"erythrocytes": 2}


@pytest.mark.asyncio
async def test_aReturnSentBackForMoreFieldsComesBackToThePhoneAndSubmitsAsReturned(db: AsyncSession) -> None:
    ids, fields = await _submittedTenFields(db)
    await _confirmedOnTheWebsite(db, ids, fields)
    notifications = NotificationService(db)
    review = ResultReviewService(
        db=db, auditLogger=AuditLogger(), _notifService=notifications,
        _smartDiagnosisService=SmartDiagnosisService(auditLogger=AuditLogger(), _notifService=notifications),
    )
    await review.returnResult(fields[0].resultId, ids.supervisorId, RETURN_REASON)
    before = await _pull(db, ids)

    reopened = await fieldService(db).reopenCapture(fields[0].resultId, ids.medtechId, None)

    assert reopened.status == ResultStatus.CAPTURING
    types = (await db.execute(
        text("SELECT notification_type FROM notifications WHERE user_id = :m"), {"m": ids.medtechId}
    )).scalars().all()
    assert "CAPTURE_REOPENED" in types
    delta = await _pull(db, ids, since=before["timestamp"])
    [result] = delta["results"]
    assert (result["status"], result["return_reason"]) == ("CAPTURING", RETURN_REASON)

    extra = await capture(db, ids)
    service = fieldService(db)
    await service.acceptField(extra.fieldId, ids.medtechId, None)
    resubmitted = await service.submitFields(fields[0].resultId, ids.medtechId, None)
    assert resubmitted.status == ResultStatus.RETURNED_FOR_CORRECTION


@pytest.mark.asyncio
async def test_rejectingAnAssignedSpecimenFromThePhoneIsStored(db: AsyncSession) -> None:
    ids = await seedAssignedSpecimen(db)

    await specimen_service.rejectSpecimen(db, ids.specimenId, ids.medtechId, "INSUFFICIENT_VOLUME", None, None)

    row = (await db.execute(
        text("SELECT status, rejection_reason, rejected_at FROM specimens WHERE specimen_id = :s"),
        {"s": ids.specimenId},
    )).one()
    assert (row.status, row.rejection_reason) == ("REJECTED", "INSUFFICIENT_VOLUME")
    assert row.rejected_at is not None
    audited = await db.scalar(
        text("SELECT count(*) FROM audit_logs WHERE event_type = 'SPECIMEN_REJECTED' AND entity_id = :s"),
        {"s": ids.specimenId},
    )
    assert audited == 1
