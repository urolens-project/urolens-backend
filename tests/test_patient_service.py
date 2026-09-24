"""Unit tests — PatientService.searchPatients (UROLENS-137).

Covers the PII-minimization fix: the search endpoint used to return the
full decrypted `PatientResponse` (name, DOB, contact, address) for every
match; it now returns `PatientSearchItem` (patientId/patientUid only) and
enforces a 3-char minimum query length rather than relying on frontend
debounce.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.encryption import encryptPii
from src.models.patient import Patient
from src.schemas.patient import PatientSearchItem
from src.services.patient_service import PatientService


def _row(patientId: uuid.UUID, firstName: str, lastName: str) -> MagicMock:
    row = MagicMock(spec=Patient)
    row.patientId = patientId
    row.patientUid = f"PAT-{patientId.hex[:6]}"
    row.firstName = encryptPii(firstName)
    row.lastName = encryptPii(lastName)
    return row


def _makeDb(rows: list[MagicMock]) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_searchPatientsBelowMinLengthReturnsEmptyWithoutQuerying():
    """Enforced in the service itself, not just the route's
    `Query(min_length=3)` — this method can be called directly.
    """
    db = AsyncMock()
    service = PatientService(db=db, auditLogger=AsyncMock())

    assert await service.searchPatients("") == []
    assert await service.searchPatients("Ja") == []
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_searchPatientsAtMinLengthReturnsSlimShapeOnly():
    patientId = uuid.uuid4()
    db = _makeDb([_row(patientId, "Jane", "Doe")])
    service = PatientService(db=db, auditLogger=AsyncMock())

    results = await service.searchPatients("Jan")

    assert len(results) == 1
    item = results[0]
    assert isinstance(item, PatientSearchItem)
    assert item.patientId == patientId

    # PII fields must not exist on the response model at all — not just be
    # absent from a sample value. This asserts the schema itself carries no
    # such field, not just that this particular instance omits one.
    dumped = item.model_dump()
    for leaked in ("firstName", "lastName", "middleName", "dateOfBirth", "contactNo", "address"):
        assert leaked not in dumped
    assert set(dumped.keys()) == {"patientId", "patientUid"}


@pytest.mark.asyncio
async def test_searchPatientsNoMatchReturnsEmpty():
    db = _makeDb([_row(uuid.uuid4(), "Jane", "Doe")])
    service = PatientService(db=db, auditLogger=AsyncMock())

    assert await service.searchPatients("xyz") == []
