"""Manual parameter override transaction (T2.6): lets a MedTech — or a
Supervisor during review — correct a single AI-generated result parameter,
recording both the original and corrected values.
"""
import uuid
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult
from ..models.manual_override import ManualOverride, OverrideKind
from ..schemas.field import FieldActionResponse
from .field_access import (
    loadFieldForWrite,
    requireFieldAccepted,
    requireVerificationPhase,
)
from .field_aggregation_service import activeOverrides, computeEffectiveCounts
from .field_correction_service import (
    currentEdits,
    loadDetections,
    markCorrected,
    overrideRowsFor,
    overrideTuples,
    recomputeEffective,
)
from .result_field_service import activeOverrideRows, buildFieldActionResponse
from .specimen_access import (
    getAssignedSpecimen,
    isMedtech,
    requireResultEditable,
    requireSpecimenNotRejected,
)


class ManualOverrideService:
    """Owns the manual parameter override transaction (T2.6).

    SRP  — one responsibility: record a MedTech's correction alongside the
            original AI value.
    DIP  — depends on injected AuditLogger.
    """

    def __init__(
        self,
        db: AsyncSession,
        auditLogger: AuditLogger,
    ) -> None:
        self.db = db
        self.auditLogger = auditLogger

    async def overrideParameter(
        self,
        resultId: uuid.UUID,
        parameter: str,
        correctedValue: int,
        rationale: str,
        originalAiValue: float | None,
        medtechId: uuid.UUID,
        callerRole: str,
        request: Request,
    ) -> ManualOverride:
        """Record a correction to one AI-detected particle count.

        The stored original is always the AI's value from `aiFindings`, never
        the client's. Every override is kept (the supervisor sees the full
        history); the latest one per parameter is the effective count.

        Args:
            originalAiValue: accepted for API-contract compatibility but never
                trusted — the stored original is read from `aiFindings`.
            medtechId: the authenticated user recorded as the override's
                author (a MedTech or a Supervisor, despite the name).
            callerRole: the caller's `role` claim. A MedTech may override
                only a specimen assigned to them, while the result is
                `PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`; a Supervisor
                only while it is `PENDING_SUPERVISOR_APPROVAL`.

        Returns:
            The persisted `ManualOverride` row.

        Raises:
            NotFoundException: `RESULT_NOT_FOUND`, if `resultId` doesn't match
                any analysis result.
            UnprocessableException: `RESULT_ALREADY_FINALISED`, if the result
                is `APPROVED` or `RELEASED`; `PARAMETER_NOT_FOUND`, if
                `parameter` isn't in the result's AI findings;
                `OVERRIDE_UNCHANGED`, if `correctedValue` equals the
                parameter's current count (its latest override, else the AI's).
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if a MedTech calls on
                a result whose specimen no longer exists.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if a MedTech calls on
                another MedTech's specimen.
            ConflictException: `SPECIMEN_REJECTED`, if a MedTech calls on a
                rejected specimen; `RESULT_NOT_EDITABLE`, if the result isn't in
                a status the caller's role may override.
        """
        result = await self._getResult(resultId)
        isSupervisor = not isMedtech(callerRole)
        if not isSupervisor:
            # Ownership before any state check, so a non-owner learns nothing;
            # then re-read under the specimen lock (see getAssignedSpecimen).
            specimen = await getAssignedSpecimen(self.db, result.specimenId, medtechId)
            requireSpecimenNotRejected(specimen)
            result = await self._getResult(resultId, fresh=True)
        requireResultEditable(result, isSupervisor)

        # Read original AI value safely from the db findings (Source of Truth)
        dbOriginalValue = await self._extractOriginalValue(result, parameter)
        await self._requireChanged(resultId, parameter, dbOriginalValue, correctedValue)

        override = ManualOverride(
            resultId=resultId,
            parameterName=parameter,
            originalAiValue=str(dbOriginalValue),
            correctedValue=str(float(correctedValue)),  # same "7.0" text format as originalAiValue
            rationale=rationale,
            medtechId=medtechId,
        )
        self.db.add(override)

        # Keep particle_classes (the confirmed/effective counts shown to the
        # Supervisor) in sync — confirm_result only settles it once, at
        # confirmation time, so any override added afterward must patch it
        # here too or the two go stale relative to each other. Reassign
        # (rather than mutate in place) so SQLAlchemy's change tracking
        # picks up the JSONB column update.
        result.particleClasses = {**(result.particleClasses or {}), parameter: correctedValue}

        # Audit logging entry block
        await self.auditLogger.record(
            eventType="RESULT_OVERRIDDEN",
            entityType="analysis_result",
            entityId=resultId,
            userId=medtechId,
            detailJson={
                "parameter":         parameter,
                "original_ai_value": dbOriginalValue,
                "corrected_value":   correctedValue,
                "specimen_id":       str(result.specimenId),
            },
            db=self.db,
            request=request,
        )

        await self.db.commit()
        await self.db.refresh(override)
        return override

    # ------------------------------------------------------------------
    # Per-field overrides (multi-field workflow)
    # ------------------------------------------------------------------

    async def overrideFieldParameter(
        self,
        fieldId: uuid.UUID,
        particleType: str,
        correctedValue: int,
        rationale: str,
        medtechId: uuid.UUID,
        request: Request,
    ) -> FieldActionResponse:
        """Set one class's count on one field by hand, with a reason.

        The original is always the field's AI count (0 if the AI found none of the
        class), never the client's. A value equal to the class's current effective
        count is refused. Inserts a `kind=OVERRIDE` row with `field_id`; values are
        stored as text like the result-level rows (`"7.0"`). While it is in force the
        class's boxes cannot be edited. Does not touch the result-level
        `overrideParameter` above.

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`;
                `INVALID_FIELD_STATUS`.
            UnprocessableException: `OVERRIDE_UNCHANGED`.
        """
        field, result, _specimen = await loadFieldForWrite(self.db, fieldId, medtechId)
        requireVerificationPhase(result)
        requireFieldAccepted(field)

        current = float((field.effectiveCounts or {}).get(particleType, 0))
        if float(correctedValue) == current:
            raise UnprocessableException(
                code="OVERRIDE_UNCHANGED",
                message="The corrected value is the same as the current value.",
            )

        now = datetime.now(UTC)
        detections = await loadDetections(self.db, field)
        edits = await currentEdits(self.db, fieldId)
        rows = await overrideRowsFor(self.db, fieldId)
        originalAi = float((field.aiFindings or {}).get(particleType, 0))
        override = ManualOverride(
            resultId=field.resultId,
            fieldId=fieldId,
            medtechId=medtechId,
            parameterName=particleType,
            originalAiValue=str(originalAi),
            correctedValue=str(float(correctedValue)),  # same "7.0" text format as originalAiValue
            rationale=rationale,
            kind=OverrideKind.OVERRIDE,
            overriddenAt=now,
        )
        self.db.add(override)
        recomputeEffective(field, detections, edits, [*rows, override])
        markCorrected(field, medtechId, now)
        await self.db.flush()

        response = await buildFieldActionResponse(self.db, fieldId)
        await self.auditLogger.record(
            eventType="RESULT_OVERRIDDEN",
            entityType="analysis_result",
            entityId=field.resultId,
            userId=medtechId,
            detailJson={
                "specimen_id": str(field.specimenId),
                "field_id": str(fieldId),
                "parameter": particleType,
                "original_ai_value": originalAi,
                "corrected_value": correctedValue,
            },
            db=self.db,
            request=request,
        )
        await self.db.commit()
        return response

    async def revertFieldOverride(
        self,
        fieldId: uuid.UUID,
        particleType: str,
        rationale: str,
        medtechId: uuid.UUID,
        request: Request,
    ) -> FieldActionResponse:
        """End the override in force for one class of one field, with a reason.

        Nothing is deleted: a `kind=REVERT` row is added and from then the class
        counts its boxes again. The row fills the NOT NULL columns: `original_ai_value`
        is the AI count and `corrected_value` the box-derived count the class returns to.

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`;
                `INVALID_FIELD_STATUS`; `OVERRIDE_NOT_IN_FORCE`.
        """
        field, result, _specimen = await loadFieldForWrite(self.db, fieldId, medtechId)
        requireVerificationPhase(result)
        requireFieldAccepted(field)

        rows = await overrideRowsFor(self.db, fieldId)
        if particleType not in activeOverrideRows(rows):
            raise ConflictException(
                code="OVERRIDE_NOT_IN_FORCE",
                message="There is no manual count in force for this class on this field.",
            )

        now = datetime.now(UTC)
        detections = await loadDetections(self.db, field)
        edits = await currentEdits(self.db, fieldId)
        othersInForce = {
            c: v for c, v in activeOverrides(overrideTuples(rows)).items() if c != particleType
        }
        boxCount = computeEffectiveCounts(
            field.aiFindings or {},
            detections,
            [e.detectionId for e in edits if e.action == "REMOVED" and e.detectionId],
            [e.particleType for e in edits if e.action == "ADDED"],
            othersInForce,
        ).get(particleType, 0)
        originalAi = float((field.aiFindings or {}).get(particleType, 0))
        revert = ManualOverride(
            resultId=field.resultId,
            fieldId=fieldId,
            medtechId=medtechId,
            parameterName=particleType,
            originalAiValue=str(originalAi),
            correctedValue=str(float(boxCount)),
            rationale=rationale,
            kind=OverrideKind.REVERT,
            overriddenAt=now,
        )
        self.db.add(revert)
        recomputeEffective(field, detections, edits, [*rows, revert])
        markCorrected(field, medtechId, now)
        await self.db.flush()

        response = await buildFieldActionResponse(self.db, fieldId)
        await self.auditLogger.record(
            eventType="OVERRIDE_REVERTED",
            entityType="analysis_result",
            entityId=field.resultId,
            userId=medtechId,
            detailJson={
                "specimen_id": str(field.specimenId),
                "field_id": str(fieldId),
                "parameter": particleType,
            },
            db=self.db,
            request=request,
        )
        await self.db.commit()
        return response

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _getResult(self, resultId: uuid.UUID, fresh: bool = False) -> AnalysisResult:
        # `fresh` overwrites the already-loaded object with current DB state.
        stmt = select(AnalysisResult).where(AnalysisResult.resultId == resultId)
        if fresh:
            stmt = stmt.execution_options(populate_existing=True)
        row = await self.db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND",
                message=f"No analysis result found with id {resultId}.",
            )
        return result

    async def _requireChanged(
        self, resultId: uuid.UUID, parameter: str, aiValue: float, correctedValue: int
    ) -> None:
        """Reject a correction that doesn't change the parameter's current count.

        The current count is the latest override's value, or the AI's when the
        parameter hasn't been overridden (UROLENS-150: a correction must
        actually differ from the current value).
        """
        latest = (
            await self.db.execute(
                select(ManualOverride.correctedValue)
                .where(ManualOverride.resultId == resultId, ManualOverride.parameterName == parameter)
                .order_by(ManualOverride.overriddenAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        current = float(latest) if latest is not None else aiValue
        if float(correctedValue) == current:
            raise UnprocessableException(
                code="OVERRIDE_UNCHANGED",
                message="The corrected value is the same as the current value.",
            )

    async def _extractOriginalValue(
        self, result: AnalysisResult, parameter: str
    ) -> float:
        """Reads the AI-generated value for the given parameter from ai_findings.
        Raises UnprocessableException if the parameter is not present.
        """
        aiFindings: dict = result.aiFindings or {}
        if parameter not in aiFindings:
            raise UnprocessableException(
                code="PARAMETER_NOT_FOUND",
                message=f"Parameter '{parameter}' not found in AI findings for this result.",
            )
        return float(aiFindings[parameter])
