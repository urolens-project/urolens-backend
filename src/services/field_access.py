"""Access and phase rules for the multi-field routes (web lane).

Reads follow `ResultReviewService.getFullResult`: a MedTech sees only a specimen
assigned to them, a Supervisor sees any result, and there is no status gate.
Writes follow the existing specimen pattern: lock the specimen, check ownership,
then read the state that matters (`getAssignedSpecimen`).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import ConflictException, ForbiddenException, NotFoundException
from ..models.analysis_result import AnalysisResult
from ..models.result_field import FieldStatus, ResultField
from ..models.specimen import Specimen
from .specimen_access import (
    MEDTECH_EDITABLE_RESULT_STATUSES,
    getAssignedSpecimen,
    isMedtech,
    requireSpecimenAssigned,
    requireSpecimenNotRejected,
)


def fieldNotFound(fieldId: uuid.UUID) -> NotFoundException:
    """404 `FIELD_NOT_FOUND`."""
    return NotFoundException(code="FIELD_NOT_FOUND", message=f"No field found with id {fieldId}.")


async def readFieldAccess(
    db: AsyncSession, specimenId: uuid.UUID, viewerId: uuid.UUID, viewerRole: str
) -> Specimen:
    """Let the caller read the fields of a result for `specimenId`.

    A MedTech may read only a specimen assigned to them; anyone else the route
    already admits (Supervisors) may read any. No lock is taken.

    Raises:
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if a MedTech asks for another
            MedTech's (or a missing) specimen.
    """
    specimen = await db.get(Specimen, specimenId)
    if isMedtech(viewerRole):
        if specimen is None:
            raise ForbiddenException(
                code="SPECIMEN_NOT_ASSIGNED", message="Specimen is not assigned to you."
            )
        requireSpecimenAssigned(specimen, viewerId)
    if specimen is None:
        # A Supervisor reading a result whose specimen row is gone: nothing to show.
        raise NotFoundException(code="SPECIMEN_NOT_FOUND", message="Specimen not found.")
    return specimen


async def loadFieldForWrite(
    db: AsyncSession, fieldId: uuid.UUID, medtechId: uuid.UUID
) -> tuple[ResultField, AnalysisResult, Specimen]:
    """Load a field for a change: lock its specimen, check ownership, re-read.

    The field is read first only to find its specimen; once the specimen is
    locked, the field and result are read again so a concurrent change cannot
    slip in between a check and the write. `detections` is not loaded.

    Raises:
        NotFoundException: `FIELD_NOT_FOUND`.
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if the specimen is not the caller's.
        ConflictException: `SPECIMEN_REJECTED`.
    """
    first = (
        await db.execute(select(ResultField.specimenId).where(ResultField.fieldId == fieldId))
    ).scalar_one_or_none()
    if first is None:
        raise fieldNotFound(fieldId)
    specimen = await getAssignedSpecimen(db, first, medtechId)
    requireSpecimenNotRejected(specimen)
    field = (
        await db.execute(
            select(ResultField)
            .where(ResultField.fieldId == fieldId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if field is None:
        raise fieldNotFound(fieldId)
    result = (
        await db.execute(
            select(AnalysisResult)
            .where(AnalysisResult.resultId == field.resultId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return field, result, specimen


def requireVerificationPhase(result: AnalysisResult) -> None:
    """The MedTech may correct a result only while it awaits verification.

    Raises:
        ConflictException: `INVALID_RESULT_STATUS`, unless the result is
            `PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`.
    """
    if result.status not in MEDTECH_EDITABLE_RESULT_STATUSES:
        raise ConflictException(
            code="INVALID_RESULT_STATUS",
            message=f"Action not allowed in status '{result.status}'.",
        )


def requireFieldAccepted(field: ResultField) -> None:
    """Corrections apply to accepted fields only.

    Raises:
        ConflictException: `INVALID_FIELD_STATUS`, unless the field is `ACCEPTED`.
    """
    if field.status != FieldStatus.ACCEPTED:
        raise ConflictException(
            code="INVALID_FIELD_STATUS",
            message=f"Action not allowed on a field in status '{field.status}'.",
        )
