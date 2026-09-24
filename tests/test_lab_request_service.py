"""Unit tests — lab_request_service.create_lab_request (consolidated)

Covers the unified implementation that now backs both the receptionist-
facing and physician-facing lab-request creation routes (see
changelog.md's "Duplicate lab-request creation implementations" entry).
Specifically pins the behavior that changed from the pre-consolidation
receptionist-only path (see git history for the prior characterization
tests this file replaced):
  - Patient existence is now validated for BOTH callers (previously only
    the physician-facing path checked this).
  - Receptionist notification fires only when notify_receptionists=True
    (the physician-facing call site); the audit log write is unconditional
    for both callers.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.core.exceptions import NotFoundException
from src.models.patient import Patient
from src.models.user import User
from src.schemas.lab_request import LabRequestCreateRequest
from src.schemas.physician import (
    LabRequestCreateRequest as PhysicianLabRequestCreateRequest,
)
from src.services.lab_request_service import (
    _generateRequestUid,
    createLabRequest,
)

PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
ENCODER_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000043")


_NOT_GIVEN = object()  # sentinel — lets `patient=None` mean "patient not found"


def _makeDb(patient=_NOT_GIVEN, getSideEffect: list | None = None) -> AsyncMock:
    """db.get(Model, id) yields [patient, *get_side_effect] in call order
    (patient lookup always happens first; defaults to a found patient unless
    `patient=None` is passed explicitly). db.execute(...) for the
    UID-collision check always reports no collision. db.flush populates
    lab_request_id (mirrors SQLAlchemy assigning the Python-side UUID
    default at flush time — needed before the notify-then-audit calls that
    read it); db.refresh populates created_at.
    """
    resolvedPatient = MagicMock(spec=Patient) if patient is _NOT_GIVEN else patient
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[resolvedPatient, *(getSideEffect or [])])
    uidCheckResult = MagicMock()
    uidCheckResult.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=uidCheckResult)
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.labRequestId = LAB_REQUEST_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    def _refresh(obj):
        obj.createdAt = "2026-08-22T00:00:00+00:00"

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.mark.asyncio
async def test_createLabRequestRaises404WhenPatientMissingReceptionistStyle():
    db = _makeDb(patient=None)
    with pytest.raises(NotFoundException) as excInfo:
        await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="urinalysis",
            clinicalNotes=None,
            physicianId=None,
            physicianName=None,
            notifyReceptionists=False,
        )
    assert excInfo.value.errorCode == "PATIENT_NOT_FOUND"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_createLabRequestRaises404WhenPatientMissingPhysicianStyle():
    """The patient-existence check now applies to the physician-style call
    path too — previously only enforced by the now-deleted
    physician_service.create_lab_request.
    """
    db = _makeDb(patient=None)
    with pytest.raises(NotFoundException) as excInfo:
        await createLabRequest(
            db,
            encodedBy=PHYSICIAN_ID,
            patientId=PATIENT_ID,
            testType="urinalysis",
            clinicalNotes=None,
            physicianId=PHYSICIAN_ID,
            physicianName="dr_santos",
            notifyReceptionists=True,
        )
    assert excInfo.value.errorCode == "PATIENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_createLabRequestReceptionistStyleSkipsNotificationButStillAudits():
    db = _makeDb()
    with patch(
        "src.services.lab_request_service.NotificationService"
    ) as mockNotifCls, patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()

        result = await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="urinalysis",
            clinicalNotes=None,
            physicianId=None,
            physicianName=None,
            notifyReceptionists=False,
            ipAddress="10.0.0.1",
        )

    mockNotifCls.assert_not_called()
    mockAuditCls.return_value.record.assert_awaited_once()
    callKwargs = mockAuditCls.return_value.record.call_args.kwargs
    assert callKwargs["eventType"] == "REQUEST_SUBMITTED"
    assert callKwargs["userId"] == ENCODER_ID
    assert callKwargs["ipAddress"] == "10.0.0.1"
    assert result.labRequestId == LAB_REQUEST_ID
    assert result.status == "PENDING_SAMPLE"


@pytest.mark.asyncio
async def test_createLabRequestPhysicianStyleNotifiesReceptionistsAndAudits():
    db = _makeDb()
    with patch(
        "src.services.lab_request_service.NotificationService"
    ) as mockNotifCls, patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockNotifCls.return_value.notifyActiveReceptionists = AsyncMock()
        mockAuditCls.return_value.record = AsyncMock()

        await createLabRequest(
            db,
            encodedBy=PHYSICIAN_ID,
            patientId=PATIENT_ID,
            testType="urinalysis",
            clinicalNotes=None,
            physicianId=PHYSICIAN_ID,
            physicianName="dr_santos",
            notifyReceptionists=True,
        )

    mockNotifCls.return_value.notifyActiveReceptionists.assert_awaited_once()
    notifyKwargs = mockNotifCls.return_value.notifyActiveReceptionists.call_args.kwargs
    assert notifyKwargs["physicianName"] == "dr_santos"
    assert notifyKwargs["labRequestId"] == LAB_REQUEST_ID
    mockAuditCls.return_value.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_createLabRequestLooksUpPhysicianNameWhenMissing():
    physician = MagicMock(spec=User)
    physician.username = "dr_santos"
    db = _makeDb(getSideEffect=[physician])

    with patch("src.services.lab_request_service.NotificationService"), patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="urinalysis",
            clinicalNotes=None,
            physicianId=PHYSICIAN_ID,
            physicianName=None,
            notifyReceptionists=False,
        )

    added = db.add.call_args[0][0]
    assert added.physicianName == "dr_santos"


@pytest.mark.asyncio
async def test_generateRequestUidRetriesThenSucceedsOnCollision():
    db = AsyncMock()
    collision = MagicMock()
    collision.scalar_one_or_none.return_value = uuid.uuid4()  # first candidate taken
    free = MagicMock()
    free.scalar_one_or_none.return_value = None  # second candidate free
    db.execute = AsyncMock(side_effect=[collision, free])

    uid = await _generateRequestUid(db)
    assert uid.startswith("REQ-")
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_generateRequestUidExhaustsRetriesRaises500():
    db = AsyncMock()
    collision = MagicMock()
    collision.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=collision)

    with pytest.raises(HTTPException) as excInfo:
        await _generateRequestUid(db)
    assert excInfo.value.status_code == 500


# ── testType: stored verbatim, no case/space mangling (UROLENS-137) ────────────


@pytest.mark.asyncio
async def test_createLabRequestStoresPredefinedTestTypeVerbatim():
    db = _makeDb()
    with patch("src.services.lab_request_service.NotificationService"), patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="Urinalysis - Complete Suite",
            clinicalNotes=None,
            physicianId=None,
            physicianName="Dr. Santos",
            notifyReceptionists=False,
        )

    added = db.add.call_args[0][0]
    assert added.testType == "Urinalysis - Complete Suite"


@pytest.mark.asyncio
async def test_createLabRequestStoresCustomOtherTestTypeVerbatim():
    """Custom "Other" free text is exactly as vulnerable to the old
    mangling as a predefined type — same assertion, different input shape.
    """
    db = _makeDb()
    with patch("src.services.lab_request_service.NotificationService"), patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="Culture & Sensitivity, Pre-Op Panel",
            clinicalNotes=None,
            physicianId=None,
            physicianName="Dr. Santos",
            notifyReceptionists=False,
        )

    added = db.add.call_args[0][0]
    assert added.testType == "Culture & Sensitivity, Pre-Op Panel"


@pytest.mark.asyncio
async def test_createLabRequestPersistsSpecialInstructionsSeparateFromClinicalNotes():
    db = _makeDb()
    with patch("src.services.lab_request_service.NotificationService"), patch(
        "src.services.lab_request_service.AuditLogger"
    ) as mockAuditCls:
        mockAuditCls.return_value.record = AsyncMock()
        await createLabRequest(
            db,
            encodedBy=ENCODER_ID,
            patientId=PATIENT_ID,
            testType="Urinalysis",
            clinicalNotes="Patient reports mild discomfort.",
            physicianId=None,
            physicianName="Dr. Santos",
            specialInstructions="Handle with care — patient has needle phobia.",
            notifyReceptionists=False,
        )

    added = db.add.call_args[0][0]
    assert added.clinicalNotes == "Patient reports mild discomfort."
    assert added.specialInstructions == "Handle with care — patient has needle phobia."


# ── Schema validation: blank testType, missing physician (UROLENS-137) ─────────


def _validLabRequestPayload(**overrides) -> dict:
    payload = {
        "patientId": PATIENT_ID,
        "physicianName": "Dr. Santos",
        "testType": "Urinalysis",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("schemaCls", [LabRequestCreateRequest, PhysicianLabRequestCreateRequest])
async def test_blankTestTypeRejected(schemaCls):
    payload = {"patientId": PATIENT_ID, "testType": "   "}
    if schemaCls is LabRequestCreateRequest:
        payload["physicianName"] = "Dr. Santos"
    with pytest.raises(ValidationError, match="testType"):
        schemaCls(**payload)


def test_testTypeIsTrimmed():
    req = LabRequestCreateRequest(**_validLabRequestPayload(testType="  Urinalysis  "))
    assert req.testType == "Urinalysis"


def test_testTypeOverMaxLengthRejected():
    with pytest.raises(ValidationError, match="testType"):
        LabRequestCreateRequest(**_validLabRequestPayload(testType="x" * 256))


def test_missingPhysicianIdentifierRejected():
    with pytest.raises(ValidationError, match="physicianId or physicianName is required"):
        LabRequestCreateRequest(
            patientId=PATIENT_ID, testType="Urinalysis", physicianId=None, physicianName=None
        )


def test_whitespaceOnlyPhysicianNameDoesNotSatisfyRequirement():
    """Trim-before-validate applies to physicianName too — " " must not
    count as a provided identifier."""
    with pytest.raises(ValidationError, match="physicianId or physicianName is required"):
        LabRequestCreateRequest(
            patientId=PATIENT_ID, testType="Urinalysis", physicianId=None, physicianName="   "
        )


def test_physicianIdAloneSatisfiesRequirement():
    req = LabRequestCreateRequest(
        patientId=PATIENT_ID,
        testType="Urinalysis",
        physicianId=PHYSICIAN_ID,
        physicianName=None,
    )
    assert req.physicianId == PHYSICIAN_ID


def test_physicianLabRequestSchemaHasNoPhysicianFields():
    """The physician-portal variant never needs the id-or-name check — the
    caller's own identity always supplies it server-side."""
    req = PhysicianLabRequestCreateRequest(patientId=PATIENT_ID, testType="Urinalysis")
    assert not hasattr(req, "physicianId")
