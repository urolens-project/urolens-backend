"""Unit tests — physician_service.searchPatients (UROLENS-152).

physician_service.searchPatients was still a pure Supabase-REST
implementation: it fetched up to 100 patient rows, decrypted every one, and
substring-matched against first/last name only — patient_uid was never part
of the match condition, so a physician searching by Patient ID (the UAC's
explicit requirement, and what the frontend's placeholder text already
promises) always got zero results.

Not the same fix as UROLENS-137: that ticket (see
tests/test_patient_service.py) minimized PatientService.searchPatients'
*response shape* (full PatientResponse -> slim PatientSearchItem) and added
a 3-char minimum query length — it never changed that path's match field,
which is still name-based today (a separate, pre-existing gap in the
receptionist-facing search, out of scope for this ticket).

Fixed here: AsyncSession (not Supabase REST), SQL-filtered on patient_uid
only (no name matching), matching 137's 3-char minimum convention. Response
shape adds dateOfBirth/sex on top of patientId/patientUid — a stated UAC
clinical-safety requirement (visually confirming the right person before
selecting), not scope creep — while still excluding name/contact/address/
clinicalHistory per RA 10173.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.encryption import encryptPii
from src.models.patient import Patient
from src.schemas.physician import PhysicianPatientItem
from src.services import physician_service


def _row(patientId: uuid.UUID, patientUid: str, dateOfBirth: str, sex: str) -> MagicMock:
    row = MagicMock(spec=Patient)
    row.patientId = patientId
    row.patientUid = patientUid
    row.dateOfBirth = encryptPii(dateOfBirth)
    row.sex = sex
    return row


def _makeSearchDb(rows: list[MagicMock]) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_searchPatientsBelowMinLengthReturnsEmptyWithoutQuerying() -> None:
    """Enforced in the service itself, not just the route's
    `Query(min_length=3)` — this function can be called directly. Matches
    UROLENS-137's exact 3-char minimum convention.
    """
    db = AsyncMock()

    assert await physician_service.searchPatients(db, "") == []
    assert await physician_service.searchPatients(db, "PA") == []
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_searchPatientsMatchesByPatientUid() -> None:
    patientId = uuid.uuid4()
    db = _makeSearchDb([_row(patientId, "PAT-000123", "2000-01-01", "FEMALE")])

    results = await physician_service.searchPatients(db, "000123")

    assert len(results) == 1
    item = results[0]
    assert isinstance(item, PhysicianPatientItem)
    assert item.patientId == patientId
    assert item.patientUid == "PAT-000123"
    assert item.dateOfBirth == "2000-01-01"
    assert item.sex == "FEMALE"


@pytest.mark.asyncio
async def test_searchPatientsOnlyFiltersOnPatientUidNeverName() -> None:
    """Confirms the old name-matching path is fully removed, not just
    deprioritized: the compiled SQL filters on patient_uid only — searching
    by a first/last name (old behavior) finds nothing, structurally, not
    just because this particular mocked row happens not to match.
    """
    db = _makeSearchDb([])

    results = await physician_service.searchPatients(db, "Jane")

    assert results == []
    stmt = db.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    whereClause = compiled.split("WHERE", 1)[1]
    assert "patient_uid" in whereClause
    assert "first_name" not in whereClause
    assert "last_name" not in whereClause


@pytest.mark.asyncio
async def test_searchPatientsNoMatchReturnsEmptyListNotError() -> None:
    db = _makeSearchDb([])

    results = await physician_service.searchPatients(db, "000999")

    assert results == []
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_searchPatientsResponseShapeExcludesNameAndContact() -> None:
    """PII fields must not exist on the response model at all — not just be
    absent from a sample value.
    """
    patientId = uuid.uuid4()
    db = _makeSearchDb([_row(patientId, "PAT-000123", "2000-01-01", "FEMALE")])

    [item] = await physician_service.searchPatients(db, "000123")
    dumped = item.model_dump()

    for leaked in ("firstName", "lastName", "middleName", "contactNo", "address", "clinicalHistory"):
        assert leaked not in dumped
    assert set(dumped.keys()) == {"patientId", "patientUid", "dateOfBirth", "sex"}
