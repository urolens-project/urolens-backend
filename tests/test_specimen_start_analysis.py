"""Unit tests — specimen_service.startAnalysis (mobile "Begin Analysis")."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.core.exceptions import ConflictException, SpecimenNotFoundError
from src.models.specimen import Specimen
from src.services import specimen_service

SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-000000000031")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000032")
OTHER_MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-000000000033")


def _makeDb(status: str = "ASSIGNED", medtechId: uuid.UUID = MEDTECH_ID, exists: bool = True):
    specimen = MagicMock(spec=Specimen)
    specimen.status = status
    specimen.medtechId = medtechId
    db = AsyncMock()
    db.get = AsyncMock(return_value=specimen if exists else None)
    db.commit = AsyncMock()
    return db, specimen


@pytest.mark.asyncio
@pytest.mark.parametrize("startStatus", ["ASSIGNED", "IN_QUEUE"])
async def test_startAnalysisMovesStartableSpecimenToProcessing(startStatus):
    db, specimen = _makeDb(status=startStatus)
    result = await specimen_service.startAnalysis(db, SPECIMEN_ID, MEDTECH_ID)
    assert specimen.status == "PROCESSING"
    assert result.status == "PROCESSING"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_startAnalysisIsIdempotentWhenAlreadyProcessing():
    db, specimen = _makeDb(status="PROCESSING")
    result = await specimen_service.startAnalysis(db, SPECIMEN_ID, MEDTECH_ID)
    assert result.status == "PROCESSING"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_startAnalysisRejectsSpecimenAssignedToAnotherMedtech():
    db, specimen = _makeDb(medtechId=OTHER_MEDTECH_ID)
    with pytest.raises(HTTPException) as exc:
        await specimen_service.startAnalysis(db, SPECIMEN_ID, MEDTECH_ID)
    assert exc.value.status_code == 403
    assert exc.value.errorCode == "SPECIMEN_NOT_ASSIGNED"
    assert specimen.status == "ASSIGNED"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("badStatus", ["REJECTED", "COMPLETED", "RECEIVED", "LABELED"])
async def test_startAnalysisConflictsOnNonStartableStatus(badStatus):
    db, specimen = _makeDb(status=badStatus)
    with pytest.raises(ConflictException) as exc:
        await specimen_service.startAnalysis(db, SPECIMEN_ID, MEDTECH_ID)
    assert exc.value.errorCode == "SPECIMEN_NOT_STARTABLE"
    assert specimen.status == badStatus


@pytest.mark.asyncio
async def test_startAnalysisRaisesNotFoundForUnknownSpecimen():
    db, _ = _makeDb(exists=False)
    with pytest.raises(SpecimenNotFoundError):
        await specimen_service.startAnalysis(db, SPECIMEN_ID, MEDTECH_ID)
