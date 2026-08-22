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

from src.urolens.core.exceptions import NotFoundException
from src.urolens.models.patient import Patient
from src.urolens.models.user import User
from src.urolens.services.lab_request_service import (
    _generate_request_uid,
    create_lab_request,
)

PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
ENCODER_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
LAB_REQUEST_ID = uuid.UUID("00000000-0000-0000-0000-000000000043")


_NOT_GIVEN = object()  # sentinel — lets `patient=None` mean "patient not found"


def _make_db(patient=_NOT_GIVEN, get_side_effect: list | None = None) -> AsyncMock:
    """db.get(Model, id) yields [patient, *get_side_effect] in call order
    (patient lookup always happens first; defaults to a found patient unless
    `patient=None` is passed explicitly). db.execute(...) for the
    UID-collision check always reports no collision. db.flush populates
    lab_request_id (mirrors SQLAlchemy assigning the Python-side UUID
    default at flush time — needed before the notify-then-audit calls that
    read it); db.refresh populates created_at.
    """
    resolved_patient = MagicMock(spec=Patient) if patient is _NOT_GIVEN else patient
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[resolved_patient, *(get_side_effect or [])])
    uid_check_result = MagicMock()
    uid_check_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=uid_check_result)
    db.add = MagicMock()

    async def _flush(objs):
        for obj in objs:
            obj.lab_request_id = LAB_REQUEST_ID

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()

    def _refresh(obj):
        obj.created_at = "2026-08-22T00:00:00+00:00"

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.mark.asyncio
async def test_create_lab_request_raises_404_when_patient_missing_receptionist_style():
    db = _make_db(patient=None)
    with pytest.raises(NotFoundException) as exc_info:
        await create_lab_request(
            db,
            encoded_by=ENCODER_ID,
            patient_id=PATIENT_ID,
            test_type="urinalysis",
            clinical_notes=None,
            physician_id=None,
            physician_name=None,
            notify_receptionists=False,
        )
    assert exc_info.value.error_code == "PATIENT_NOT_FOUND"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_create_lab_request_raises_404_when_patient_missing_physician_style():
    """The patient-existence check now applies to the physician-style call
    path too — previously only enforced by the now-deleted
    physician_service.create_lab_request.
    """
    db = _make_db(patient=None)
    with pytest.raises(NotFoundException) as exc_info:
        await create_lab_request(
            db,
            encoded_by=PHYSICIAN_ID,
            patient_id=PATIENT_ID,
            test_type="urinalysis",
            clinical_notes=None,
            physician_id=PHYSICIAN_ID,
            physician_name="dr_santos",
            notify_receptionists=True,
        )
    assert exc_info.value.error_code == "PATIENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_create_lab_request_receptionist_style_skips_notification_but_still_audits():
    db = _make_db()
    with patch(
        "src.urolens.services.lab_request_service.NotificationService"
    ) as mock_notif_cls, patch(
        "src.urolens.services.lab_request_service.AuditLogger"
    ) as mock_audit_cls:
        mock_audit_cls.return_value.record = AsyncMock()

        result = await create_lab_request(
            db,
            encoded_by=ENCODER_ID,
            patient_id=PATIENT_ID,
            test_type="urinalysis",
            clinical_notes=None,
            physician_id=None,
            physician_name=None,
            notify_receptionists=False,
            ip_address="10.0.0.1",
        )

    mock_notif_cls.assert_not_called()
    mock_audit_cls.return_value.record.assert_awaited_once()
    call_kwargs = mock_audit_cls.return_value.record.call_args.kwargs
    assert call_kwargs["event_type"] == "REQUEST_SUBMITTED"
    assert call_kwargs["user_id"] == ENCODER_ID
    assert call_kwargs["ip_address"] == "10.0.0.1"
    assert result.lab_request_id == LAB_REQUEST_ID
    assert result.status == "PENDING_SAMPLE"


@pytest.mark.asyncio
async def test_create_lab_request_physician_style_notifies_receptionists_and_audits():
    db = _make_db()
    with patch(
        "src.urolens.services.lab_request_service.NotificationService"
    ) as mock_notif_cls, patch(
        "src.urolens.services.lab_request_service.AuditLogger"
    ) as mock_audit_cls:
        mock_notif_cls.return_value.notify_active_receptionists = AsyncMock()
        mock_audit_cls.return_value.record = AsyncMock()

        await create_lab_request(
            db,
            encoded_by=PHYSICIAN_ID,
            patient_id=PATIENT_ID,
            test_type="urinalysis",
            clinical_notes=None,
            physician_id=PHYSICIAN_ID,
            physician_name="dr_santos",
            notify_receptionists=True,
        )

    mock_notif_cls.return_value.notify_active_receptionists.assert_awaited_once()
    notify_kwargs = mock_notif_cls.return_value.notify_active_receptionists.call_args.kwargs
    assert notify_kwargs["physician_name"] == "dr_santos"
    assert notify_kwargs["lab_request_id"] == LAB_REQUEST_ID
    mock_audit_cls.return_value.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_lab_request_looks_up_physician_name_when_missing():
    physician = MagicMock(spec=User)
    physician.username = "dr_santos"
    db = _make_db(get_side_effect=[physician])

    with patch("src.urolens.services.lab_request_service.NotificationService"), patch(
        "src.urolens.services.lab_request_service.AuditLogger"
    ) as mock_audit_cls:
        mock_audit_cls.return_value.record = AsyncMock()
        await create_lab_request(
            db,
            encoded_by=ENCODER_ID,
            patient_id=PATIENT_ID,
            test_type="urinalysis",
            clinical_notes=None,
            physician_id=PHYSICIAN_ID,
            physician_name=None,
            notify_receptionists=False,
        )

    added = db.add.call_args[0][0]
    assert added.physician_name == "dr_santos"


@pytest.mark.asyncio
async def test_generate_request_uid_retries_then_succeeds_on_collision():
    db = AsyncMock()
    collision = MagicMock()
    collision.scalar_one_or_none.return_value = uuid.uuid4()  # first candidate taken
    free = MagicMock()
    free.scalar_one_or_none.return_value = None  # second candidate free
    db.execute = AsyncMock(side_effect=[collision, free])

    uid = await _generate_request_uid(db)
    assert uid.startswith("REQ-")
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_generate_request_uid_exhausts_retries_raises_500():
    db = AsyncMock()
    collision = MagicMock()
    collision.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=collision)

    with pytest.raises(HTTPException) as exc_info:
        await _generate_request_uid(db)
    assert exc_info.value.status_code == 500
