"""Manual parameter override transaction (T2.6): lets a MedTech — or a
Supervisor during review — correct a single AI-generated result parameter,
recording both the original and corrected values.
"""
import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import (
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult
from ..models.manual_override import ManualOverride
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
