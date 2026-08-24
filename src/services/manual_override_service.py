"""Manual parameter override transaction (T2.6): lets a MedTech correct a
single AI-generated result parameter, recording both the original and
corrected values.
"""
import uuid

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..core.exceptions import NotFoundException, UnprocessableException
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.manual_override import ManualOverride


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
        originalAiValue: float,  # Accepted here to match your router argument contract
        medtechId: uuid.UUID,
        request: Request,
    ) -> ManualOverride:
        """Records a MedTech correction for a single AI-generated parameter.
        The system uses the db-extracted original value as a secure source of truth.

        Args:
            original_ai_value: accepted to match the router's argument
                contract but not trusted — the value actually stored is
                read fresh from the DB via `_extract_original_value`.
            medtech_id: the authenticated user recorded as the override's author.

        Returns:
            The persisted `ManualOverride` row.

        Raises:
            NotFoundException: `result_id` doesn't match any analysis result.
            UnprocessableException: the result has already been finalised
                (`APPROVED`/`RETURNED_FOR_CORRECTION`), or `parameter` isn't
                present in the result's AI findings.
        """
        result = await self._getResult(resultId)

        # Guard: overrides only allowed before Supervisor approval
        if result.status in (ResultStatus.APPROVED, ResultStatus.RETURNED_FOR_CORRECTION):
            raise UnprocessableException(
                code="RESULT_ALREADY_FINALISED",
                message="Cannot override a parameter after the result has been finalised.",
            )

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

    async def _getResult(self, resultId: uuid.UUID) -> AnalysisResult:
        stmt = select(AnalysisResult).where(AnalysisResult.resultId == resultId)
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
