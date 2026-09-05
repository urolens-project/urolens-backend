"""Physician-portal result listing/detail — SQLAlchemy `AsyncSession`
implementation, scoped to results for patients the requesting physician has
an associated lab request for.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.audit_logger import AuditLogger
from src.core.config import settings
from src.core.encryption import decryptPii
from src.core.exceptions import NotFoundException
from src.models.analysis_result import AnalysisResult
from src.models.image import Image
from src.models.lab_request import LabRequest
from src.models.patient import Patient
from src.models.result_retrieval import ResultRetrieval
from src.models.result_review import ResultReview
from src.models.smart_diagnosis_output import SmartDiagnosisOutput
from src.models.specimen import Specimen
from src.models.user import User
from src.schemas.physician import (
    PhysicianResultDetail,
    PhysicianResultListResponse,
    PhysicianResultSummary,
    SmartDiagnosisDetail,
)

_PHT = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)

# Status shown to a physician for a result that hasn't reached RELEASED yet —
# same "released/not-released" masking as PatientResultService.get_patient_results,
# not the raw internal workflow state. See get_result_detail's matching gate.
_PENDING_PLACEHOLDER_STATUS = "PENDING"


def _computeAge(dobStr: str | None) -> int | None:
    # Computes age in whole years from an ISO date-of-birth string;
    # returns None if unset or unparseable.
    if not dobStr:
        return None
    try:
        dob = date.fromisoformat(dobStr[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _imagePublicUrl(storageKey: str | None) -> str | None:
    # Builds the public Supabase storage URL for a specimen image; returns
    # None if there's no storage key or no configured Supabase URL.
    if not storageKey or not settings.supabaseUrl:
        return None
    base = settings.supabaseUrl.rstrip("/")
    return f"{base}/storage/v1/object/public/{settings.supabaseImageBucket}/{storageKey}"


class PhysicianResultService:
    """Read-side operations for the physician portal's result list/detail
    views, scoped to patients the requesting physician has a lab request for.
    """

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        self.db = db
        self.auditLogger = auditLogger

    async def _getPhysicianPatientIds(self, physicianId: uuid.UUID) -> set[uuid.UUID]:
        # Distinct patient IDs from lab requests attributed to this physician —
        # the access-scoping set used by list_results/get_result_detail.
        stmt = select(LabRequest.patientId).where(LabRequest.physicianId == physicianId)
        rows = (await self.db.execute(stmt)).scalars().all()
        return {patientId for patientId in rows if patientId}

    async def listResults(
        self, physicianId: uuid.UUID, page: int, pageSize: int
    ) -> PhysicianResultListResponse:
        """List analysis results for patients associated with this physician's
        lab requests, newest-created first.

        A result that hasn't reached `RELEASED` yet is shown with the
        `"PENDING"` placeholder status rather than its raw internal workflow
        state — matching `get_result_detail`'s `RESULT_NOT_RELEASED` gate.

        Args:
            physician_id: the authenticated physician; results are scoped to
                patients from this physician's own lab requests.
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            `items` (list of `PhysicianResultSummary`), `total` (matching row
            count), `page`, and `page_size`.
        """
        patientIds = await self._getPhysicianPatientIds(physicianId)
        if not patientIds:
            return PhysicianResultListResponse(items=[], total=0, page=page, pageSize=pageSize)

        offset = (page - 1) * pageSize

        countStmt = select(func.count()).select_from(AnalysisResult).where(
            AnalysisResult.patientId.in_(patientIds)
        )
        pageStmt = (
            select(AnalysisResult)
            .where(AnalysisResult.patientId.in_(patientIds))
            .order_by(AnalysisResult.createdAt.desc())
            .offset(offset)
            .limit(pageSize)
        )
        total = (await self.db.execute(countStmt)).scalar_one()
        arRows = (await self.db.execute(pageStmt)).scalars().all()
        if not arRows:
            return PhysicianResultListResponse(items=[], total=total, page=page, pageSize=pageSize)

        specimenIds = {r.specimenId for r in arRows if r.specimenId}
        dbPatientIds = {r.patientId for r in arRows if r.patientId}

        specRows = (
            await self.db.execute(select(Specimen).where(Specimen.specimenId.in_(specimenIds)))
        ).scalars().all()
        patRows = (
            await self.db.execute(select(Patient).where(Patient.patientId.in_(dbPatientIds)))
        ).scalars().all()
        specMap = {s.specimenId: s for s in specRows}
        patMap = {p.patientId: p for p in patRows}

        items: list[PhysicianResultSummary] = []
        for ar in arRows:
            spec = specMap.get(ar.specimenId)
            pat = patMap.get(ar.patientId)

            try:
                first = decryptPii(pat.firstName) if pat and pat.firstName else ""
                last = decryptPii(pat.lastName) if pat and pat.lastName else ""
                dob = decryptPii(pat.dateOfBirth) if pat and pat.dateOfBirth else None
            except Exception:
                first = last = ""
                dob = None
            patientName = f"{first} {last}".strip() or (spec.patientName if spec else "") or ""

            items.append(PhysicianResultSummary(
                resultId=str(ar.resultId),
                specimenId=str(ar.specimenId) if ar.specimenId else "",
                patientName=patientName,
                patientUid=(spec.patientUid if spec else None)
                or (pat.patientUid if pat else None)
                or "",
                patientAge=_computeAge(dob),
                patientSex=pat.sex if pat else None,
                status=ar.status if ar.status == "RELEASED" else _PENDING_PLACEHOLDER_STATUS,
                confirmedAt=ar.confirmedAt.isoformat() if ar.confirmedAt else None,
                createdAt=ar.createdAt.isoformat() if ar.createdAt else "",
            ))

        return PhysicianResultListResponse(items=items, total=total, page=page, pageSize=pageSize)

    async def getResultDetail(
        self,
        resultId: uuid.UUID,
        physicianId: uuid.UUID,
        request: Request,
    ) -> PhysicianResultDetail:
        """Fetch one result's full detail for the physician portal, verifying
        the physician has access via their own lab requests, and logging the
        retrieval (both a `result_retrievals` row and an audit entry).

        Args:
            physician_id: the authenticated physician; access is denied unless
                this physician has a lab request for the result's patient.

        Returns:
            A `PhysicianResultDetail` with patient info, findings, smart
            diagnosis (if attached), and image URL.

        Raises:
            HTTPException: 404, if `result_id` doesn't exist. 403, if the
                result's patient isn't among this physician's own patients
                (including when the result has no `patient_id` at all — a
                result unlinked to any patient can't belong to any
                physician's own patient set either). 403
                (`RESULT_NOT_RELEASED`), if the result exists and is
                accessible but hasn't reached `RELEASED` status yet.
        """
        ar = await self.db.get(AnalysisResult, resultId)
        if ar is None:
            raise NotFoundException(code="RESULT_NOT_FOUND", message="Analysis result not found.")

        # Ownership check: previously skipped entirely when patient_id was
        # falsy (`if patient_id:` guarded the whole block), which let any
        # physician retrieve any patient-less result with no check at all.
        # Now always evaluated — a missing patient_id denies access instead
        # of bypassing the check.
        patientId = ar.patientId
        physicianPatientIds = await self._getPhysicianPatientIds(physicianId)
        if not patientId or patientId not in physicianPatientIds:
            exc = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")
            exc.errorCode = "ACCESS_DENIED"
            raise exc

        if ar.status != "RELEASED":
            exc = HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Result is not yet released.",
            )
            exc.errorCode = "RESULT_NOT_RELEASED"
            raise exc

        spec = await self.db.get(Specimen, ar.specimenId) if ar.specimenId else None

        sdoRows = (
            await self.db.execute(
                select(SmartDiagnosisOutput).where(SmartDiagnosisOutput.resultId == resultId)
            )
        ).scalars().all()

        annotationNotes = (
            await self.db.execute(
                select(ResultReview.annotationNotes)
                .where(ResultReview.resultId == resultId)
                .limit(1)
            )
        ).scalar_one_or_none()

        pat = await self.db.get(Patient, patientId)

        medtechName: str | None = None
        if spec is not None and spec.medtechId:
            medtech = await self.db.get(User, spec.medtechId)
            medtechName = medtech.username if medtech else None

        imageUrl: str | None = None
        if ar.imageId:
            image = await self.db.get(Image, ar.imageId)
            imageUrl = _imagePublicUrl(image.storageKey if image else None)

        try:
            first = decryptPii(pat.firstName) if pat and pat.firstName else ""
            last = decryptPii(pat.lastName) if pat and pat.lastName else ""
            dob = decryptPii(pat.dateOfBirth) if pat and pat.dateOfBirth else None
            sex = pat.sex if pat else None
        except Exception:
            first = last = ""
            dob = sex = None
        patientName = f"{first} {last}".strip() or (spec.patientName if spec else "") or ""

        smartDiagnosis: SmartDiagnosisDetail | None = None
        if not ar.smartDiagnosisUnavailable and sdoRows and sdoRows[0].status == "ATTACHED":
            sdo = sdoRows[0]
            smartDiagnosis = SmartDiagnosisDetail(
                goutScore=sdo.goutScore,
                gnScore=sdo.gnScore,
                nephroScore=sdo.nephroScore,
                # uti_score/tricho_score were renamed to gn_score/nephro_score
                # by migration 0017 and no longer exist as separate columns —
                # matches this schema's own long-standing effective behavior
                # (the pre-migration Supabase-REST code read the old column
                # names via .get(..., "LOW"), which the rename had already
                # made a permanent miss).
                utiScore="LOW",
                trichoScore="LOW",
                evidenceMap=sdo.evidenceMap or {},
                noSignificantIndicators=sdo.noSignificantIndicators,
                engineVersion=sdo.engineVersion,
            )

        ipAddress = request.client.host if request.client else "unknown"
        retrievedAt = datetime.now(_PHT)

        retrieval = ResultRetrieval(
            resultId=resultId,
            physicianId=physicianId,
            retrievedAt=retrievedAt,
            ipAddress=ipAddress,
        )
        self.db.add(retrieval)

        await self.auditLogger.record(
            eventType="RESULT_RETRIEVED",
            entityType="analysis_result",
            entityId=resultId,
            userId=physicianId,
            request=request,
            ipAddress=ipAddress,
        )

        await self.db.commit()

        return PhysicianResultDetail(
            resultId=str(ar.resultId),
            specimenId=str(ar.specimenId) if ar.specimenId else "",
            patientName=patientName,
            patientUid=(spec.patientUid if spec else None)
            or (pat.patientUid if pat else None)
            or "",
            patientAge=_computeAge(dob),
            patientSex=sex,
            medtechName=medtechName,
            confirmedAt=ar.confirmedAt.isoformat() if ar.confirmedAt else None,
            aiFindings=ar.aiFindings or {},
            flaggedAnomalies=ar.flaggedAnomalies or {},
            particleClasses=ar.particleClasses or {},
            modelVersion=ar.modelVersion or "",
            smartDiagnosis=smartDiagnosis,
            imageUrl=imageUrl,
            status=ar.status,
            annotationNotes=annotationNotes,
        )
