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
        audit_logger: AuditLogger,
    ) -> None:
        self.db = db
        self.audit_logger = audit_logger

    async def override_parameter(
        self,
        result_id: uuid.UUID,
        parameter: str,
        corrected_value: float,
        rationale: str,
        original_ai_value: float,  # Accepted here to match your router argument contract
        medtech_id: uuid.UUID,
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
        result = await self._get_result(result_id)

        # Guard: overrides only allowed before Supervisor approval
        if result.status in (ResultStatus.APPROVED, ResultStatus.RETURNED_FOR_CORRECTION):
            raise UnprocessableException(
                code="RESULT_ALREADY_FINALISED",
                message="Cannot override a parameter after the result has been finalised.",
            )

        # Read original AI value safely from the db findings (Source of Truth)
        db_original_value = await self._extract_original_value(result, parameter)

        override = ManualOverride(
            result_id=result_id,
            parameter_name=parameter,
            original_ai_value=str(db_original_value),
            corrected_value=str(corrected_value),
            rationale=rationale,
            medtech_id=medtech_id,
        )
        self.db.add(override)

        # Audit logging entry block
        await self.audit_logger.record(
            event_type="RESULT_OVERRIDDEN",
            entity_type="analysis_result",
            entity_id=result_id,
            user_id=medtech_id,
            detail_json={
                "parameter":         parameter,
                "original_ai_value": db_original_value,
                "corrected_value":   corrected_value,
                "specimen_id":       str(result.specimen_id),
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

    async def _get_result(self, result_id: uuid.UUID) -> AnalysisResult:
        stmt = select(AnalysisResult).where(AnalysisResult.result_id == result_id)
        row = await self.db.execute(stmt)
        result = row.scalar_one_or_none()
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND",
                message=f"No analysis result found with id {result_id}.",
            )
        return result

    async def _extract_original_value(
        self, result: AnalysisResult, parameter: str
    ) -> float:
        """Reads the AI-generated value for the given parameter from ai_findings.
        Raises UnprocessableException if the parameter is not present.
        """
        ai_findings: dict = result.ai_findings or {}
        if parameter not in ai_findings:
            raise UnprocessableException(
                code="PARAMETER_NOT_FOUND",
                message=f"Parameter '{parameter}' not found in AI findings for this result.",
            )
        return float(ai_findings[parameter])
