"""Manual parameter override transaction (T2.6): lets a MedTech — or a
Supervisor during review — correct a single AI-generated result parameter,
recording both the original and corrected values.
"""
import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.enums import UserRole
from ..core.exceptions import (
    NotFoundException,
    UnprocessableException,
)
from ..models.analysis_result import AnalysisResult
from ..models.manual_override import ManualOverride
from .specimen_access import getAssignedSpecimen, requireResultEditable


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
        correctedValue: float,
        rationale: str,
        originalAiValue: float | None,  # Accepted here to match your router argument contract
        medtechId: uuid.UUID,
        callerRole: str,
        request: Request,
    ) -> ManualOverride:
        """Records a MedTech correction for a single AI-generated parameter.
        The system uses the db-extracted original value as a secure source of truth.

        Args:
            original_ai_value: accepted to match the router's argument
                contract but not trusted (and not required) — the value
                actually stored is read fresh from the DB via
                `_extract_original_value`.
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
                `parameter` isn't in the result's AI findings.
            SpecimenNotFoundError: `SPECIMEN_NOT_FOUND`, if a MedTech calls on
                a result whose specimen no longer exists.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, if a MedTech calls on
                another MedTech's specimen.
            ConflictException: `RESULT_NOT_EDITABLE`, if the result isn't in
                a status the caller's role may override.
        """
        result = await self._getResult(resultId)
        isSupervisor = callerRole.upper() == UserRole.SUPERVISOR
        if not isSupervisor:
            # Ownership before any state check, so a non-owner learns nothing;
            # then re-read under the specimen lock (see getAssignedSpecimen).
            await getAssignedSpecimen(self.db, result.specimenId, medtechId)
            result = await self._getResult(resultId, fresh=True)
        requireResultEditable(result, isSupervisor)

        # Read original AI value safely from the db findings (Source of Truth)
        dbOriginalValue = await self._extractOriginalValue(result, parameter)

        override = ManualOverride(
            resultId=resultId,
            parameterName=parameter,
            originalAiValue=str(dbOriginalValue),
            correctedValue=str(correctedValue),
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
