"""
Characterization tests — physician_service.create_lab_request (pre-consolidation)

Pins the CURRENT behavior of the physician-facing Supabase-REST
implementation before it's deleted in favor of the unified
lab_request_service.create_lab_request (see changelog.md's "Duplicate
lab-request creation implementations" flagged finding). Covers the three
behaviors this path has that its receptionist-facing twin currently lacks:
patient-existence validation, receptionist notification fan-out, and audit
logging — the latter two are best-effort and must never propagate a
failure back to the caller.

search_patients is untouched by the consolidation and isn't covered here.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from src.urolens.schemas.physician import LabRequestCreateRequest
from src.urolens.services.physician_service import create_lab_request

PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000050")
PHYSICIAN_ID = "00000000-0000-0000-0000-000000000051"
PHYSICIAN_USERNAME = "dr_reyes"


def _table_mock(select_execute=None, insert_execute=None) -> MagicMock:
    """Mock one `supabase.table(name)` result. Supports a
    `.select(...).eq(...)[.eq(...)][.maybe_single()].execute()` chain (its
    own AsyncMock at `select_execute`) independently from an
    `.insert(...).execute()` chain (`insert_execute`) — the two chains never
    share a terminal `.execute()` mock, so call order between them doesn't
    matter."""
    table = MagicMock()

    select_chain = MagicMock()
    select_chain.select.return_value = select_chain
    select_chain.eq.return_value = select_chain
    select_chain.maybe_single.return_value = select_chain
    select_chain.execute = select_execute or AsyncMock(return_value=MagicMock(data=None))
    table.select = select_chain.select

    insert_chain = MagicMock()
    insert_chain.insert.return_value = insert_chain
    insert_chain.execute = insert_execute or AsyncMock(return_value=MagicMock(data=[]))
    table.insert = insert_chain.insert

    return table


def _make_supabase(
    *,
    patient_found: bool = True,
    uid_collision: bool = False,
    insert_result: MagicMock | None = None,
    receptionist_ids: list[str] | None = None,
    notifications_execute=None,
    audit_execute=None,
) -> MagicMock:
    patients_data = {"patient_id": str(PATIENT_ID)} if patient_found else None
    lab_request_row = {"lab_request_id": str(uuid.uuid4())}
    tables = {
        "patients": _table_mock(
            select_execute=AsyncMock(return_value=MagicMock(data=patients_data))
        ),
        "lab_requests": _table_mock(
            select_execute=AsyncMock(
                return_value=MagicMock(data=[{"request_uid": "REQ-COLLIDE"}] if uid_collision else [])
            ),
            insert_execute=(
                AsyncMock(return_value=insert_result)
                if insert_result is not None
                else AsyncMock(return_value=MagicMock(data=[lab_request_row]))
            ),
        ),
        "users": _table_mock(
            select_execute=AsyncMock(
                return_value=MagicMock(
                    data=[{"user_id": rid} for rid in (receptionist_ids or [])]
                )
            )
        ),
        "notifications": _table_mock(insert_execute=notifications_execute),
        "audit_logs": _table_mock(insert_execute=audit_execute),
    }
    supabase = MagicMock()
    supabase.table.side_effect = lambda name: tables[name]
    return supabase


def _make_request() -> MagicMock:
    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "10.0.0.5"
    return request


@pytest.mark.asyncio
async def test_create_lab_request_patient_not_found_raises_404():
    supabase = _make_supabase(patient_found=False)
    payload = LabRequestCreateRequest(patient_id=PATIENT_ID, test_type="urinalysis")

    with patch("src.urolens.services.physician_service.supabase", supabase):
        with pytest.raises(HTTPException) as exc_info:
            await create_lab_request(payload, PHYSICIAN_ID, PHYSICIAN_USERNAME, _make_request())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_lab_request_happy_path_notifies_receptionists_and_audits():
    supabase = _make_supabase(receptionist_ids=[str(uuid.uuid4()), str(uuid.uuid4())])
    payload = LabRequestCreateRequest(
        patient_id=PATIENT_ID, test_type="urinalysis", clinical_notes="fasting sample"
    )

    with patch("src.urolens.services.physician_service.supabase", supabase):
        result = await create_lab_request(payload, PHYSICIAN_ID, PHYSICIAN_USERNAME, _make_request())

    assert result["patient_id"] == PATIENT_ID
    assert result["physician_name"] == PHYSICIAN_USERNAME
    assert result["test_type"] == "URINALYSIS"
    assert result["status"] == "PENDING_SAMPLE"
    assert result["request_uid"].startswith("REQ-")

    # Notification fan-out actually happened (one bulk insert of 2 rows).
    notifications_table = supabase.table("notifications")
    notifications_table.insert.assert_called_once()
    inserted_notifications = notifications_table.insert.call_args[0][0]
    assert len(inserted_notifications) == 2
    assert all(n["notification_type"] == "LAB_REQUEST_SUBMITTED" for n in inserted_notifications)

    # Audit log actually happened.
    audit_table = supabase.table("audit_logs")
    audit_table.insert.assert_called_once()
    audit_payload = audit_table.insert.call_args[0][0]
    assert audit_payload["event_type"] == "REQUEST_SUBMITTED"
    assert audit_payload["user_id"] == PHYSICIAN_ID


@pytest.mark.asyncio
async def test_create_lab_request_notification_failure_is_swallowed():
    failing_execute = AsyncMock(side_effect=RuntimeError("notifications table unreachable"))
    supabase = _make_supabase(
        receptionist_ids=[str(uuid.uuid4())], notifications_execute=failing_execute
    )
    payload = LabRequestCreateRequest(patient_id=PATIENT_ID, test_type="urinalysis")

    with patch("src.urolens.services.physician_service.supabase", supabase):
        result = await create_lab_request(payload, PHYSICIAN_ID, PHYSICIAN_USERNAME, _make_request())

    assert result["status"] == "PENDING_SAMPLE"


@pytest.mark.asyncio
async def test_create_lab_request_audit_failure_is_swallowed():
    failing_execute = AsyncMock(side_effect=RuntimeError("audit_logs table unreachable"))
    supabase = _make_supabase(audit_execute=failing_execute)
    payload = LabRequestCreateRequest(patient_id=PATIENT_ID, test_type="urinalysis")

    with patch("src.urolens.services.physician_service.supabase", supabase):
        result = await create_lab_request(payload, PHYSICIAN_ID, PHYSICIAN_USERNAME, _make_request())

    assert result["status"] == "PENDING_SAMPLE"


@pytest.mark.asyncio
async def test_create_lab_request_insert_failure_raises_500():
    supabase = _make_supabase(insert_result=MagicMock(data=[]))
    payload = LabRequestCreateRequest(patient_id=PATIENT_ID, test_type="urinalysis")

    with patch("src.urolens.services.physician_service.supabase", supabase):
        with pytest.raises(HTTPException) as exc_info:
            await create_lab_request(payload, PHYSICIAN_ID, PHYSICIAN_USERNAME, _make_request())
    assert exc_info.value.status_code == 500
