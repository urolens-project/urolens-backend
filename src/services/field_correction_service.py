"""MedTech corrections to a field during verification (web lane): saving box edits
and marking a field as checked. Numeric overrides live in `ManualOverrideService`.

Every real correction does the same bookkeeping (`markCorrected`): the field's
`version` goes up, `last_corrected_at` is set, the caller is recorded as having
opened the field at that same moment (so they can mark it checked straight away),
the verified mark is cleared and `effective_counts` is rewritten. The AI output
(`ai_findings`, `detections`) is never edited.
"""
from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    FieldNotOpenedError,
    FieldVersionConflictError,
    OverrideInForceError,
    UnprocessableException,
)
from ..models.field_annotation_edit import EditAction, FieldAnnotationEdit
from ..models.manual_override import ManualOverride
from ..models.result_field import ResultField
from ..schemas.field import MAX_FIELD_COUNT, FieldActionResponse, SaveAnnotationsRequest
from .field_access import (
    loadFieldForWrite,
    requireFieldAccepted,
    requireVerificationPhase,
)
from .field_aggregation_service import activeOverrides, computeEffectiveCounts
from .result_field_service import buildFieldActionResponse

_BOUNDS_EPSILON = 1e-6


def markCorrected(field: ResultField, medtechId: uuid.UUID, now: datetime) -> None:
    """The bookkeeping every correction does (rules R4 and Q6)."""
    field.version += 1
    field.lastCorrectedAt = now
    field.lastOpenedAt = now
    field.lastOpenedBy = medtechId
    field.verifiedBy = None
    field.verifiedAt = None


def overrideTuples(rows: Sequence[ManualOverride]) -> list[tuple[str, str, Any, datetime]]:
    """Override rows as the `(particleType, kind, correctedValue, overriddenAt)` tuples
    `activeOverrides` reads."""
    return [(r.parameterName, r.kind, r.correctedValue, r.overriddenAt) for r in rows]


def recomputeEffective(
    field: ResultField,
    detections: Sequence[Mapping[str, Any]],
    edits: Sequence[FieldAnnotationEdit],
    overrideRows: Sequence[ManualOverride],
) -> dict[str, float]:
    """Rewrite `field.effectiveCounts` from the AI counts, the edits and the overrides in force."""
    effective = computeEffectiveCounts(
        field.aiFindings or {},
        detections,
        [e.detectionId for e in edits if e.action == EditAction.REMOVED and e.detectionId],
        [e.particleType for e in edits if e.action == EditAction.ADDED],
        activeOverrides(overrideTuples(overrideRows)),
    )
    field.effectiveCounts = effective
    return effective


async def currentEdits(db: AsyncSession, fieldId: uuid.UUID) -> list[FieldAnnotationEdit]:
    """The field's edits that have not been superseded, oldest first."""
    return list(
        (
            await db.execute(
                select(FieldAnnotationEdit)
                .where(
                    FieldAnnotationEdit.fieldId == fieldId,
                    FieldAnnotationEdit.supersededAt.is_(None),
                )
                .order_by(FieldAnnotationEdit.editedAt)
            )
        ).scalars().all()
    )


async def overrideRowsFor(db: AsyncSession, fieldId: uuid.UUID) -> list[ManualOverride]:
    """Every override and revert row of the field, oldest first."""
    return list(
        (
            await db.execute(
                select(ManualOverride)
                .where(ManualOverride.fieldId == fieldId)
                .order_by(ManualOverride.overriddenAt)
            )
        ).scalars().all()
    )


async def loadDetections(db: AsyncSession, field: ResultField) -> list[dict[str, Any]]:
    """The field's AI detections (deferred, so loaded here on request)."""
    await db.refresh(field, attribute_names=["detections"])
    return list(field.detections or [])


def _boxKey(particleType: str, x: float, y: float, w: float, h: float) -> tuple[str, float, float, float, float]:
    # Stored as REAL (float32); compare to 3 decimals so a round trip does not look like an edit.
    return (particleType, round(float(x), 3), round(float(y), 3), round(float(w), 3), round(float(h), 3))


class FieldCorrectionService:
    """Box edits and the checked mark for one request."""

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        """Bind the request-scoped session and audit logger."""
        self.db = db
        self.auditLogger = auditLogger

    async def saveAnnotations(
        self,
        fieldId: uuid.UUID,
        medtechId: uuid.UUID,
        body: SaveAnnotationsRequest,
        request: Request,
    ) -> FieldActionResponse:
        """Replace the field's box edits with `body`'s full list.

        `body.expectedVersion` must equal the field's `version`, else nothing is
        written (409 `FIELD_VERSION_CONFLICT`; the specimen lock serialises
        concurrent saves). Every removed id must be one of the field's AI detections.
        Added boxes are percent of the upright image and must fit inside it. The
        current edit rows are marked superseded and the new set is inserted; rows
        are never deleted. A list identical to the stored one changes nothing
        (no version bump, no audit row). Edits to a class that has an override in
        force are refused.

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`;
                `INVALID_FIELD_STATUS`; `FIELD_VERSION_CONFLICT`; `OVERRIDE_IN_FORCE`.
            UnprocessableException: `DETECTION_NOT_FOUND`, `BOX_OUT_OF_BOUNDS`,
                `TOO_MANY_BOXES`.
        """
        field, result, _specimen = await loadFieldForWrite(self.db, fieldId, medtechId)
        requireVerificationPhase(result)
        requireFieldAccepted(field)
        if body.expectedVersion != field.version:
            raise FieldVersionConflictError()

        detections = await loadDetections(self.db, field)
        classById = {str(d.get("id")): str(d.get("particleType")) for d in detections}

        removedIds = list(body.removedDetectionIds)
        if len(set(removedIds)) != len(removedIds):
            raise UnprocessableException(
                code="DETECTION_NOT_FOUND", message="A detection is listed more than once."
            )
        unknown = [i for i in removedIds if i not in classById]
        if unknown:
            raise UnprocessableException(
                code="DETECTION_NOT_FOUND",
                message=f"{len(unknown)} removed detection(s) do not belong to this field.",
            )
        if len(body.addedBoxes) > MAX_FIELD_COUNT:
            raise UnprocessableException(
                code="TOO_MANY_BOXES",
                message=f"A field can have at most {MAX_FIELD_COUNT} added boxes.",
            )
        for box in body.addedBoxes:
            if box.x + box.w > 100 + _BOUNDS_EPSILON or box.y + box.h > 100 + _BOUNDS_EPSILON:
                raise UnprocessableException(
                    code="BOX_OUT_OF_BOUNDS", message="A box extends past the edge of the image."
                )

        edits = await currentEdits(self.db, fieldId)
        overrideRows = await overrideRowsFor(self.db, fieldId)
        inForce = activeOverrides(overrideTuples(overrideRows))

        currentRemoved = {e.detectionId for e in edits if e.action == EditAction.REMOVED}
        currentAdded = Counter(
            _boxKey(e.particleType, e.x, e.y, e.w, e.h)  # type: ignore[arg-type]
            for e in edits if e.action == EditAction.ADDED
        )
        newRemoved = set(removedIds)
        newAdded = Counter(
            _boxKey(b.particleType, b.x, b.y, b.w, b.h) for b in body.addedBoxes
        )
        if newRemoved == currentRemoved and newAdded == currentAdded:
            return await buildFieldActionResponse(self.db, fieldId)

        for particleType in inForce:
            removedChanged = {i for i in newRemoved ^ currentRemoved if classById.get(i) == particleType}
            addedChanged = {
                key for key in (newAdded.keys() | currentAdded.keys())
                if key[0] == particleType and newAdded[key] != currentAdded[key]
            }
            if removedChanged or addedChanged:
                raise OverrideInForceError()

        now = datetime.now(UTC)
        await self.db.execute(
            update(FieldAnnotationEdit)
            .where(
                FieldAnnotationEdit.fieldId == fieldId,
                FieldAnnotationEdit.supersededAt.is_(None),
            )
            .values(supersededAt=now)
            .execution_options(synchronize_session=False)
        )
        newEdits = [
            FieldAnnotationEdit(
                editId=uuid.uuid4(), fieldId=fieldId, action=EditAction.REMOVED,
                detectionId=detectionId, particleType=classById[detectionId],
                editedBy=medtechId, editedAt=now,
            )
            for detectionId in removedIds
        ] + [
            FieldAnnotationEdit(
                editId=uuid.uuid4(), fieldId=fieldId, action=EditAction.ADDED,
                particleType=b.particleType, x=b.x, y=b.y, w=b.w, h=b.h,
                editedBy=medtechId, editedAt=now,
            )
            for b in body.addedBoxes
        ]
        for edit in newEdits:
            self.db.add(edit)
        recomputeEffective(field, detections, newEdits, overrideRows)
        markCorrected(field, medtechId, now)
        await self.db.flush()

        response = await buildFieldActionResponse(self.db, fieldId)
        await self.auditLogger.record(
            eventType="FIELD_ANNOTATIONS_SAVED",
            entityType="analysis_result",
            entityId=field.resultId,
            userId=medtechId,
            detailJson={
                "specimen_id": str(field.specimenId),
                "field_id": str(fieldId),
                "added": len(body.addedBoxes),
                "removed": len(removedIds),
            },
            db=self.db,
            request=request,
        )
        await self.db.commit()
        return response

    async def markVerified(
        self, fieldId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> FieldActionResponse:
        """Mark the field as checked by the caller.

        Allowed only if the caller opened the field since its last correction:
        `last_opened_by` is the caller and `last_opened_at >= coalesce(last_corrected_at,
        created_at)`. A correction by the caller counts as an open, so they can verify
        straight after saving; an open or correction by anyone else does not help.
        Verifying is not a correction (no version bump). A field already verified
        (corrections clear the mark) returns its current state.

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`;
                `INVALID_FIELD_STATUS`; `FIELD_NOT_OPENED`.
        """
        field, result, _specimen = await loadFieldForWrite(self.db, fieldId, medtechId)
        requireVerificationPhase(result)
        requireFieldAccepted(field)
        if field.verifiedBy is not None:
            return await buildFieldActionResponse(self.db, fieldId)

        since = field.lastCorrectedAt or field.createdAt
        if (
            field.lastOpenedBy != medtechId
            or field.lastOpenedAt is None
            or field.lastOpenedAt < since
        ):
            raise FieldNotOpenedError()

        field.verifiedBy = medtechId
        field.verifiedAt = datetime.now(UTC)
        await self.db.flush()

        response = await buildFieldActionResponse(self.db, fieldId)
        await self.auditLogger.record(
            eventType="FIELD_VERIFIED",
            entityType="analysis_result",
            entityId=field.resultId,
            userId=medtechId,
            detailJson={"specimen_id": str(field.specimenId), "field_id": str(fieldId)},
            db=self.db,
            request=request,
        )
        await self.db.commit()
        return response
