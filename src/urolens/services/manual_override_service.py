# Path: urolens-backend/src/urolens/services/manual_override_service.py
import uuid
from datetime import datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.manual_override import ManualOverride
from ..core.audit_logger import AuditLogger
from ..core.exceptions import NotFoundException, UnprocessableException


class ManualOverrideService:
    """
    Owns the manual parameter override transaction (T2.6).

    SRP  — one responsibility: record a MedTech's correction alongside the
           original AI value. It does not re-run inference or alter the
           result status.
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
        medtech_id: uuid.UUID,
        request: Request,
    ) -> ManualOverride:
        """
        Records a MedTech correction for a single AI-generated parameter.

        The original AI value is read from the current ai_findings and stored
        permanently in original_ai_value. It is NEVER overwritten.

        Raises:
            NotFoundException: result_id does not exist.
            UnprocessableException: parameter not present in ai_findings,
                                   or result already confirmed/approved.
        """
        result = await self._get_result(result_id)

        # Guard: overrides only allowed before Supervisor approval
        if result.status in (ResultStatus.APPROVED, ResultStatus.RETURNED):
            raise UnprocessableException(
                code="RESULT_ALREADY_FINALISED",
                message="Cannot override a parameter after the result has been finalised.",
            )

        # Read original AI value before any write — preserved permanently
        original_ai_value = await self._extract_original_value(result, parameter)

        override = ManualOverride(
            result_id=result_id,
            parameter=parameter,
            original_ai_value=original_ai_value,   # preserved — never mutated
            corrected_value=corrected_value,
            rationale=rationale,
            overridden_by=medtech_id,
            overridden_at=datetime.utcnow(),
        )
        self.db.add(override)

        # Audit — same transaction (LSP: same call shape as every other service)
        await self.audit_logger.record(
            event_type="RESULT_OVERRIDDEN",
            entity_type="analysis_result",
            entity_id=result_id,
            user_id=medtech_id,
            detail_json={
                "parameter":         parameter,
                "original_ai_value": original_ai_value,
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
        stmt = select(AnalysisResult).where(AnalysisResult.id == result_id)
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
        """
        Reads the AI-generated value for the given parameter from ai_findings.
        Raises UnprocessableException if the parameter is not present.
        """
        ai_findings: dict = result.ai_findings or {}
        if parameter not in ai_findings:
            raise UnprocessableException(
                code="PARAMETER_NOT_FOUND",
                message=f"Parameter '{parameter}' not found in AI findings for this result.",
            )
        return float(ai_findings[parameter])