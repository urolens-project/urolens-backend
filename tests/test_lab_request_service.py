"""
Characterization tests — lab_request_service.create_lab_request (pre-consolidation)

Pins the CURRENT behavior of the receptionist-facing SQLAlchemy
implementation before it's folded into the unified consolidation with
physician_service.create_lab_request (its Supabase-REST twin — see
changelog.md's "Duplicate lab-request creation implementations" flagged
finding). In particular, this file documents two gaps the consolidation
closes, not correct behavior:
  - No patient-existence check is performed today — a bad patient_id is not
    caught here at all (see test_create_lab_request_does_not_check_patient_existence).
  - No notification or audit log write happens on this path today, unlike
    its physician-facing twin.
These tests are rewritten once create_lab_request moves to the unified
signature.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.urolens.models.lab_request import LabRequest
from src.urolens.models.user import User
from src.urolens.schemas.lab_request import LabRequestCreateRequest
from src.urolens.services.lab_request_service import create_lab_request, _generate_request_uid

PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
PHYSICIAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000041")
ENCODER_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")


def _make_db(get_side_effect: list | None = None) -> AsyncMock:
    """db.execute(...) for the UID-collision check always reports no
    collision (scalar_one_or_none() -> None); db.get(...) yields
    get_side_effect items in order, for the physician-name lookup."""
    db = AsyncMock()
    uid_check_result = MagicMock()
    uid_check_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=uid_check_result)
    db.get = AsyncMock(side_effect=get_side_effect or [])
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_create_lab_request_does_not_check_patient_existence():
    """Characterizes today's gap: an unknown patient_id is not validated
    before insert — no NotFound-style exception is raised here. (The
    physician-facing twin *does* perform this check; consolidating folds it
    into this path too.)"""
    db = _make_db()
    payload = LabRequestCreateRequest(
        patient_id=PATIENT_ID, test_type="urinalysis", clinical_notes=None
    )

    response = await create_lab_request(db, ENCODER_ID, payload)

    assert response.success is True
    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, LabRequest)
    assert added.patient_id == PATIENT_ID
    assert added.encoded_by == ENCODER_ID
    assert added.status == "PENDING_SAMPLE"
    assert added.test_type == "URINALYSIS"


@pytest.mark.asyncio
async def test_create_lab_request_looks_up_physician_name_when_missing():
    physician = MagicMock(spec=User)
    physician.username = "dr_santos"
    db = _make_db(get_side_effect=[physician])
    payload = LabRequestCreateRequest(
        patient_id=PATIENT_ID, physician_id=PHYSICIAN_ID, test_type="urinalysis"
    )

    await create_lab_request(db, ENCODER_ID, payload)

    db.get.assert_called_once_with(User, PHYSICIAN_ID)
    added = db.add.call_args[0][0]
    assert added.physician_name == "dr_santos"


@pytest.mark.asyncio
async def test_create_lab_request_physician_lookup_miss_leaves_name_none():
    db = _make_db(get_side_effect=[None])
    payload = LabRequestCreateRequest(
        patient_id=PATIENT_ID, physician_id=PHYSICIAN_ID, test_type="urinalysis"
    )

    await create_lab_request(db, ENCODER_ID, payload)

    added = db.add.call_args[0][0]
    assert added.physician_name is None


@pytest.mark.asyncio
async def test_create_lab_request_neither_physician_field_given():
    db = _make_db()
    payload = LabRequestCreateRequest(patient_id=PATIENT_ID, test_type="urinalysis")

    await create_lab_request(db, ENCODER_ID, payload)

    db.get.assert_not_called()
    added = db.add.call_args[0][0]
    assert added.physician_id is None
    assert added.physician_name is None


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
