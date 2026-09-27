"""Shared MedTech access rules for specimen-scoped write actions (SEC-2).

One place for "is this specimen yours?" and "can this result still be
edited?", used by specimen reject/start-analysis, image upload/discard, result
confirmation and manual override. Before this, only reject/start-analysis
checked ownership, so any MedTech could act on any other MedTech's specimen
through the other four (security audit F-03/F-04/F-08).
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ForbiddenException, SpecimenNotFoundError
from src.models.analysis_result import ResultStatus
from src.models.specimen import Specimen

# The only statuses in which a MedTech may still change a result (override a
# parameter, discard its image, replace its image): before it is submitted,
# or after a supervisor sends it back. Once submitted, approved or released,
# only the supervisor workflow may touch it.
MEDTECH_EDITABLE_RESULT_STATUSES = frozenset({
    ResultStatus.PENDING_CONFIRM,
    ResultStatus.RETURNED_FOR_CORRECTION,
})

# A result's image may be discarded or replaced in the editable statuses,
# plus the two that exist precisely to ask for a new image.
MEDTECH_IMAGE_REPLACEABLE_RESULT_STATUSES = MEDTECH_EDITABLE_RESULT_STATUSES | frozenset({
    ResultStatus.IMAGE_RETAKE_REQUESTED,
    ResultStatus.FAILED,
})


def requireSpecimenAssigned(specimen: Specimen, medtechId: uuid.UUID) -> None:
    """Reject the caller unless the specimen is assigned to them.

    Raises:
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if `specimen.medtechId`
            isn't `medtechId` (including an unassigned specimen).
    """
    if specimen.medtechId != medtechId:
        raise ForbiddenException(
            code="SPECIMEN_NOT_ASSIGNED",
            message="Specimen is not assigned to you.",
        )


async def getAssignedSpecimen(
    db: AsyncSession, specimenId: uuid.UUID, medtechId: uuid.UUID
) -> Specimen:
    """Load and lock a specimen the calling MedTech is assigned to.

    The row is locked (`SELECT ... FOR UPDATE`) until the caller's
    transaction ends, so every specimen-scoped write — reject, start,
    upload, discard, confirm, override — runs one at a time per specimen.
    Callers must read any state they check (the result's status, the
    image's status) *after* this call, or a concurrent write can slip in
    between the check and the change (e.g. an upload resetting a result
    that was confirmed from another device mid-upload).

    Returns:
        The `Specimen` row.

    Raises:
        SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if it doesn't exist.
        ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if it isn't the caller's.
    """
    specimen = await db.get(Specimen, specimenId, with_for_update=True)
    if specimen is None:
        raise SpecimenNotFoundError(str(specimenId))
    requireSpecimenAssigned(specimen, medtechId)
    return specimen
