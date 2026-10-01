"""Unit tests — processing-consent gate (UROLENS-222, RA 10173).

`requireProcessingConsent` on its own, then wired into image upload and
result confirmation. The patient's latest consent decides: refused ->
409 `CONSENT_REFUSED`; given -> allowed silently; none on file -> allowed and
audited as `CONSENT_NOT_ON_FILE` in the action's transaction. DB is mocked.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from src.core.exceptions import ConflictException
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.consent import Consent
from src.models.specimen import Specimen
from src.services.ai_integration_service import AIIntegrationService
from src.services.consent_check import requireProcessingConsent
from src.services.result_confirmation_service import ResultConfirmationService

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b4")
RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b5")


def _makeSpecimen(labRequestId: uuid.UUID | None = LAB_REQUEST_ID) -> Specimen:
    specimen = MagicMock(spec=Specimen)
    specimen.specimenId = SPECIMEN_ID
    specimen.labRequestId = labRequestId
    specimen.medtechId = MEDTECH_ID
    specimen.status = "PROCESSING"
    return specimen


def _makeConsent(processing: bool) -> Consent:
    consent = MagicMock(spec=Consent)
    consent.consentProcess = processing
    return consent


def _makeDb(patientId: uuid.UUID | None = PATIENT_ID, consent: Consent | None = None) -> AsyncMock:
    # db.scalar answers the patient lookup, then the latest-consent lookup.
    db = AsyncMock()
    db.scalar = AsyncMock(side_effect=[patientId, consent])
    return db


def _makeAuditLogger() -> MagicMock:
    auditLogger = MagicMock()
    auditLogger.record = AsyncMock()
    return auditLogger


# ── requireProcessingConsent ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refusedProcessingConsentBlocksTheAction():
    auditLogger = _makeAuditLogger()

    with pytest.raises(ConflictException) as excInfo:
        await requireProcessingConsent(
            _makeDb(consent=_makeConsent(processing=False)),
            _makeSpecimen(), MEDTECH_ID, auditLogger, action="IMAGE_UPLOAD",
        )

    assert excInfo.value.status_code == 409
    assert excInfo.value.errorCode == "CONSENT_REFUSED"
    auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_givenProcessingConsentAllowsTheActionWithoutAnAuditEntry():
    auditLogger = _makeAuditLogger()

    await requireProcessingConsent(
        _makeDb(consent=_makeConsent(processing=True)),
        _makeSpecimen(), MEDTECH_ID, auditLogger, action="IMAGE_UPLOAD",
    )

    auditLogger.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_missingConsentIsAllowedButAuditedInTheActionsTransaction():
    # 13 of 38 live patients predate consent capture; refusing would block them.
    db = _makeDb(consent=None)
    auditLogger = _makeAuditLogger()

    await requireProcessingConsent(
        db, _makeSpecimen(), MEDTECH_ID, auditLogger, action="RESULT_CONFIRM", request="req"
    )

    auditLogger.record.assert_awaited_once()
    kwargs = auditLogger.record.call_args.kwargs
    assert kwargs["eventType"] == "CONSENT_NOT_ON_FILE"
    assert kwargs["entityId"] == SPECIMEN_ID
    assert kwargs["userId"] == MEDTECH_ID
    assert kwargs["db"] is db  # commits/rolls back with the action
    assert kwargs["detailJson"] == {"action": "RESULT_CONFIRM", "patient_id": str(PATIENT_ID)}


@pytest.mark.asyncio
async def test_specimenWithoutALabRequestCountsAsNoConsentOnFile():
    db = AsyncMock()
    db.scalar = AsyncMock()
    auditLogger = _makeAuditLogger()

    await requireProcessingConsent(
        db, _makeSpecimen(labRequestId=None), MEDTECH_ID, auditLogger, action="IMAGE_UPLOAD"
    )

    db.scalar.assert_not_awaited()
    assert auditLogger.record.call_args.kwargs["detailJson"]["patient_id"] is None


@pytest.mark.asyncio
async def test_theLatestConsentRecordIsTheOneThatCounts():
    # A patient who consented and later withdrew must be treated as refused.
    db = _makeDb(consent=_makeConsent(processing=False))

    with pytest.raises(ConflictException):
        await requireProcessingConsent(
            db, _makeSpecimen(), MEDTECH_ID, _makeAuditLogger(), action="IMAGE_UPLOAD"
        )

    consentQuery = db.scalar.await_args_list[1].args[0]
    sql = str(consentQuery.compile(dialect=postgresql.dialect())).lower()
    assert "order by consents.recorded_at desc" in sql
    assert "limit" in sql


# ── Wired into upload and confirm ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uploadIsRefusedWhenThePatientRefusedProcessing():
    db = _makeDb(consent=_makeConsent(processing=False))
    db.get = AsyncMock(return_value=_makeSpecimen())
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = None  # no result yet
    db.execute = AsyncMock(return_value=executeResult)
    service = AIIntegrationService(db=db, auditLogger=_makeAuditLogger())

    with pytest.raises(ConflictException) as excInfo:
        await service._requireUploadAllowed(SPECIMEN_ID, MEDTECH_ID)

    assert excInfo.value.errorCode == "CONSENT_REFUSED"


@pytest.mark.asyncio
async def test_confirmIsRefusedWhenThePatientRefusedProcessing():
    result = MagicMock(spec=AnalysisResult)
    result.resultId = RESULT_ID
    result.specimenId = SPECIMEN_ID
    result.status = ResultStatus.PENDING_CONFIRM
    result.manualOverrides = []
    result.confirmedBy = None
    result.imageId = None
    db = _makeDb(consent=_makeConsent(processing=False))
    db.get = AsyncMock(return_value=_makeSpecimen())
    executeResult = MagicMock()
    executeResult.scalar_one_or_none.return_value = result
    db.execute = AsyncMock(return_value=executeResult)
    db.add = MagicMock()
    service = ResultConfirmationService(
        db=db, auditLogger=_makeAuditLogger(), _smartDiagnosisService=MagicMock(), _notifService=MagicMock()
    )

    with pytest.raises(ConflictException) as excInfo:
        await service.confirmResult(resultId=RESULT_ID, medtechId=MEDTECH_ID, request=MagicMock())

    assert excInfo.value.errorCode == "CONSENT_REFUSED"
    assert result.status == ResultStatus.PENDING_CONFIRM
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
