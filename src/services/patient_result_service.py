"""Patient-portal result listing/detail — SQLAlchemy `AsyncSession`
implementation.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.models.analysis_result import AnalysisResult
from src.models.patient import Patient
from src.models.result_view import ResultView
from src.schemas.patient_portal import (
    PARTICLE_LABELS,
    ParticleCount,
    PatientResultDetailResponse,
    PatientResultItem,
)

# Status shown to a patient for a result that hasn't reached RELEASED yet —
# the internal workflow states (PENDING_CONFIRM, PENDING_SUPERVISOR_APPROVAL,
# APPROVED, RETURNED_FOR_CORRECTION, CRITICAL_ESCALATED, IMAGE_RETAKE_REQUESTED,
# FAILED) are lab-internal detail a patient has no use for and shouldn't see
# broken out — collapsed to one placeholder so the list still tells the
# released/not-released story getResultDetail's status gate enforces, without
# leaking which internal review stage a result is in.
_PENDING_PLACEHOLDER_STATUS = "PENDING"


class PatientResultService:
    """Read-side operations for the patient portal's result list/detail views."""

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger):
        self.db = db
        self.auditLogger = auditLogger

    async def _resolvePatientId(self, userId: UUID) -> UUID:
        # Maps an authenticated portal user_id to their patient_id.
        # Raises HTTPException 404 (PATIENT_NOT_FOUND) if no patient row exists for this user.
        stmt = select(Patient.patientId).where(Patient.userId == userId)
        patientId = (await self.db.execute(stmt)).scalar_one_or_none()
        if patientId is None:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient record not found for this account.",
            )
            exc.errorCode = "PATIENT_NOT_FOUND"
            raise exc
        return patientId

    async def getPatientResults(self, userId: UUID) -> list[PatientResultItem]:
        """List the authenticated patient's analysis results, newest-released first.

        A result that hasn't reached `RELEASED` yet is shown with the
        `"PENDING"` placeholder status rather than its raw internal workflow
        state — matching the access boundary `get_result_detail` enforces
        (see its `RESULT_NOT_RELEASED` gate).

        Returns:
            One `PatientResultItem` per result, ordered by `released_at` descending.

        Raises:
            HTTPException: 404 (`PATIENT_NOT_FOUND`), if no patient record is
                linked to this user account.
        """
        patientId = await self._resolvePatientId(userId)

        stmt = (
            select(AnalysisResult)
            .where(AnalysisResult.patientId == patientId)
            .order_by(AnalysisResult.releasedAt.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()

        return [
            PatientResultItem(
                resultId=row.resultId,
                testType="Urinalysis",
                status=row.status if row.status == "RELEASED" else _PENDING_PLACEHOLDER_STATUS,
                releasedAt=row.releasedAt,
            )
            for row in rows
        ]

    async def getResultDetail(
        self, resultId: UUID, userId: UUID, request: Request
    ) -> PatientResultDetailResponse:
        """Fetch one result's full detail for the patient portal, recording
        the view (a `result_views` row plus a `RESULT_VIEWED` audit entry).

        Also used internally by the PDF-download route — the `RELEASED`
        gate below applies to that path too.

        Args:
            user_id: the authenticated portal user; the result must belong to
                this user's own patient record.

        Returns:
            The result detail, including particle counts normalised against
            the fixed `PARTICLE_LABELS` set.

        Raises:
            HTTPException: 404 (`PATIENT_NOT_FOUND`), if no patient record is
                linked to this user account. 404 (`RESULT_NOT_FOUND`), if
                `result_id` doesn't exist. 403 (`ACCESS_DENIED`), if the
                result belongs to a different patient. 403
                (`RESULT_NOT_RELEASED`), if the result exists and belongs to
                this patient but hasn't reached `RELEASED` status yet.
        """
        patientId = await self._resolvePatientId(userId)

        row = await self.db.get(AnalysisResult, resultId)

        if row is None:
            exc = HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Result not found.",
            )
            exc.errorCode = "RESULT_NOT_FOUND"
            raise exc

        rowPatientId = row.patientId
        if not rowPatientId or patientId != rowPatientId:
            exc = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied.",
            )
            exc.errorCode = "ACCESS_DENIED"
            raise exc

        if row.status != "RELEASED":
            exc = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Result is not yet released.",
            )
            exc.errorCode = "RESULT_NOT_RELEASED"
            raise exc

        self.db.add(ResultView(resultId=resultId, patientId=patientId))

        await self.auditLogger.record(
            eventType="RESULT_VIEWED",
            entityType="analysis_result",
            entityId=row.resultId,
            userId=userId,
            request=request,
        )

        await self.db.commit()

        # Normalise ai_findings → exactly 10 ParticleCount rows
        rawFindings: dict = row.aiFindings or {}
        particleCounts = [
            ParticleCount(label=label, count=int(rawFindings.get(label, 0)))
            for label in PARTICLE_LABELS
        ]

        # Extract particle_classes — stored as JSONB (dict or list)
        rawClasses = row.particleClasses or {}
        if isinstance(rawClasses, list):
            particleClasses = [str(c) for c in rawClasses]
        elif isinstance(rawClasses, dict):
            particleClasses = list(rawClasses.keys())
        else:
            particleClasses = []

        return PatientResultDetailResponse(
            status=row.status,
            confirmedAt=row.confirmedAt,
            confirmationNotes=row.interpretation,
            analyzedBy=row.medtechName,
            particleCounts=particleCounts,
            particleClasses=particleClasses,
            smartDiagnosisUnavailable=bool(row.smartDiagnosisUnavailable),
            testType="Urinalysis",
            releasedAt=row.releasedAt,
        )
