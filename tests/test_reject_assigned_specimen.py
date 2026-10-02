"""Unit tests — a MedTech rejecting an assigned specimen (UROLENS-238).

- The rejection writes a `SPECIMEN_REJECTED` audit row in its transaction,
  stores a trimmed note (blank → none) and a UTC timestamp.
- Every active receptionist is notified after the rejection commits (sample ID
  only); a notification failure never undoes the rejection.
- A rejected specimen is closed: override, annotation and image discard are
  refused with `SPECIMEN_REJECTED`.
- The online MedTech queue leaves rejected specimens out.
DB is mocked.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from src.core.enums import UserRole
from src.core.exceptions import ConflictException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.audit_log import AuditLog
from src.models.image import Image
from src.models.specimen import Specimen
from src.schemas.specimen import SpecimenRejectRequest
from src.services import specimen_service
from src.services.image_retake_service import ImageRetakeService
from src.services.manual_override_service import ManualOverrideService
from src.services.notification_service import NotificationService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_review_service import ResultReviewService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a1")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a2")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000004a3")


def _makeSpecimen(status: str = "PROCESSING") -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.sampleUid = "SMP-20260930-00077"
    specimen.medtechId = MEDTECH_ID
    specimen.status = status
    specimen.rejectedAt = None
    return specimen


def _rejectDb(specimen: Specimen, resultStatus: ResultStatus | None = None) -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=resultStatus)))
    db.add = MagicMock()
    return db


def _auditRows(db: AsyncMock) -> list[AuditLog]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]


async def _reject(db: AsyncMock, note: str | None = "Container cracked", notify: AsyncMock | None = None) -> object:
    notify = notify or AsyncMock()
    with patch.object(specimen_service, "NotificationService") as notificationService:
        notificationService.return_value.notifyReceptionistsSpecimenRejected = notify
        return await specimen_service.rejectSpecimen(db, SPECIMEN_ID, MEDTECH_ID, "OTHER", note)


# ── Audit, note, timestamp ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rejectionIsAuditedInItsTransaction() -> None:
    db = _rejectDb(_makeSpecimen(status="PROCESSING"), ResultStatus.RETURNED_FOR_CORRECTION)

    await _reject(db)

    [audit] = _auditRows(db)
    assert audit.eventType == "SPECIMEN_REJECTED"
    assert audit.entityType == "specimen"
    assert audit.detailJson == {
        "reason": "OTHER", "has_note": True, "previous_status": "PROCESSING",
        "result_status": "RETURNED_FOR_CORRECTION",
    }
    # Added before the first commit, so it commits with the rejection.
    assert db.method_calls.index(next(c for c in db.method_calls if c[0] == "add")) < db.method_calls.index(
        next(c for c in db.method_calls if c[0] == "commit")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("note", "stored"), [("  Leaking lid  ", "Leaking lid"), ("   ", None), (None, None)])
async def test_theNoteIsTrimmedAndABlankNoteIsNoNote(note: str | None, stored: str | None) -> None:
    specimen = _makeSpecimen()
    db = _rejectDb(specimen)

    await _reject(db, note=note)

    assert specimen.rejectionNote == stored
    assert _auditRows(db)[0].detailJson["has_note"] is (stored is not None)


@pytest.mark.asyncio
async def test_theRejectionTimeIsStoredInUtc() -> None:
    specimen = _makeSpecimen()

    response = await _reject(_rejectDb(specimen))

    assert specimen.rejectedAt.utcoffset().total_seconds() == 0
    assert response.rejectedAt.endswith("+00:00")


def test_theNoteIsCappedAtFiveHundredCharacters() -> None:
    assert SpecimenRejectRequest(reasonCode="OTHER", freeTextNote="x" * 500).freeTextNote
    with pytest.raises(ValidationError):
        SpecimenRejectRequest(reasonCode="OTHER", freeTextNote="x" * 501)


# ── Receptionists are notified ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receptionistsAreNotifiedAfterTheRejectionCommits() -> None:
    order: list[str] = []
    db = _rejectDb(_makeSpecimen())
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    notify = AsyncMock(side_effect=lambda **kwargs: order.append("notify"))

    await _reject(db, notify=notify)

    notify.assert_awaited_once_with(specimenId=SPECIMEN_ID, sampleUid="SMP-20260930-00077", reason="other")
    assert order == ["commit", "notify", "commit"]


@pytest.mark.asyncio
async def test_aNotificationFailureNeverUndoesTheRejection() -> None:
    specimen = _makeSpecimen()
    db = _rejectDb(specimen)

    response = await _reject(db, notify=AsyncMock(side_effect=RuntimeError("push service down")))

    assert response.status == "REJECTED"
    assert specimen.status == "REJECTED"
    db.commit.assert_awaited_once()  # the rejection's own commit
    db.rollback.assert_awaited_once()  # only the notification attempt is dropped


@pytest.mark.asyncio
async def test_everyActiveReceptionistGetsTheSampleIdButNoPatientDetails() -> None:
    receptionists = [uuid.uuid4(), uuid.uuid4()]
    service = NotificationService(db=AsyncMock())

    with patch.object(service, "_getActiveUserIds", AsyncMock(return_value=receptionists)) as activeUsers, \
         patch.object(service, "notify", AsyncMock()) as notify:
        await service.notifyReceptionistsSpecimenRejected(
            specimenId=SPECIMEN_ID, sampleUid="SMP-20260930-00077", reason="wrong container"
        )

    activeUsers.assert_awaited_once_with(UserRole.RECEPTIONIST)
    assert [c.kwargs["userId"] for c in notify.await_args_list] == receptionists
    call = notify.await_args_list[0].kwargs
    assert call["notificationType"] == "SPECIMEN_REJECTED"
    assert call["entityId"] == SPECIMEN_ID
    assert "SMP-20260930-00077" in call["message"] and "wrong container" in call["message"]
    assert "new specimen" in call["message"]


# ── A rejected specimen is closed ─────────────────────────────────────────────

def _result() -> AnalysisResult:
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_CONFIRM
    result.aiFindings = {"erythrocytes": 7}
    result.particleClasses = {}
    return result


def _closedDb() -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(return_value=_makeSpecimen(status="REJECTED"))
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=_result())))
    db.add = MagicMock()
    return db


async def _override(db: AsyncMock) -> None:
    await ManualOverrideService(db=db, auditLogger=MagicMock(record=AsyncMock())).overrideParameter(
        resultId=RESULT_ID, parameter="erythrocytes", correctedValue=4, rationale="Recounted",
        originalAiValue=None, medtechId=MEDTECH_ID, callerRole="MEDTECH", request=MagicMock(spec=Request),
    )


async def _annotate(db: AsyncMock) -> None:
    await ResultReviewService(db=db, auditLogger=MagicMock(record=AsyncMock())).saveAnnotation(
        resultId=RESULT_ID, userId=MEDTECH_ID, callerRole="MEDTECH", annotationNotes="note",
    )


async def _discard(db: AsyncMock) -> None:
    image = MagicMock(spec=Image)
    image.specimenId = SPECIMEN_ID
    image.status = "ACTIVE"
    db.get = AsyncMock(side_effect=lambda model, *a, **k: image if model is Image else _makeSpecimen("REJECTED"))
    await ImageRetakeService(db=db, auditLogger=MagicMock(record=AsyncMock())).discardAndRetake(
        imageId=uuid.uuid4(), medtechId=MEDTECH_ID
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [_override, _annotate, _discard], ids=["override", "annotate", "discard"])
async def test_nothingMoreCanBeDoneWithARejectedSpecimen(action: object) -> None:
    db = _closedDb()

    with pytest.raises(ConflictException) as excInfo:
        await action(db)

    assert excInfo.value.status_code == 409
    assert excInfo.value.errorCode == "SPECIMEN_REJECTED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


# ── The online queue ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_theMedtechQueueLeavesRejectedSpecimensOut() -> None:
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one=MagicMock(return_value=0)),
        MagicMock(all=MagicMock(return_value=[])),
    ])
    service = ResultConfirmationService(
        db=db, auditLogger=MagicMock(), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )

    await service._queryPendingForMedtech(MEDTECH_ID, 1, 20, None, "oldest")

    for call in db.execute.await_args_list:  # both the count and the page
        compiled = call.args[0].compile(dialect=postgresql.dialect())
        assert "specimens.status != " in str(compiled)
        assert "REJECTED" in compiled.params.values()
