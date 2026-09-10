"""Supervisor review/approval service — consolidation plan row 7.

SQLAlchemy port of app/services/result_review_service.py (636 lines, pure
Supabase REST, deleted after this port — see CHANGELOG.md). Tier-1 workflow
per the standards skill (the confirm->override->approve->release chain's
named example).

One field from the original service is still not persisted/populated here,
reported rather than guessed at — see CHANGELOG.md for the full
schema-drift finding:
  - AnalysisResult.confirmation_notes (no Alembic history, already dead
    going forward since the canonical confirm path never wrote it)

`ResultReview.spatial_annotations` was in the same situation (no Alembic
history, and `save_annotation` silently dropped every caller-supplied
value — a real regression, not just a documentation gap) but has since
been fixed: mapped as JSONB (migration 0034) and persisted. See the
`ResultReview` model's docstring and CHANGELOG.md for the type-inference
reasoning and the pending live-schema-verification caveat.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.encryption import decryptPii
from ..core.exceptions import (
    ConflictException,
    NotFoundException,
    UnprocessableException,
)
from ..core.supabase import supabase
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.escalation import Escalation
from ..models.image import Image
from ..models.manual_override import ManualOverride
from ..models.patient import Patient
from ..models.result_approval import ResultApproval
from ..models.result_return import ResultReturn
from ..models.result_review import ResultReview
from ..models.smart_diagnosis_output import SmartDiagnosisOutput
from ..models.specimen import Specimen
from ..models.user import User
from ..schemas.result_review import VALID_ESCALATION_PATHS

_PHT = timezone(timedelta(hours=8))
_ALLOWED_STATUSES_FOR_ACTION = {ResultStatus.PENDING_SUPERVISOR_APPROVAL}


def _computeAge(dobStr: str | None) -> int | None:
    """Age in whole years from an ISO date string, or None if unparseable."""
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


def _decryptOrNone(ciphertext: str | None) -> str | None:
    # Decrypts PII, returning None (rather than raising) for an unset or
    # undecryptable value.
    if not ciphertext:
        return None
    try:
        return decryptPii(ciphertext)
    except Exception:
        return None


class ResultReviewService:
    """Owns the supervisor review/approval workflow: pending queue, approved/
    escalated lists, full result detail, annotation, and the
    approve/return/escalate transitions.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Private helpers ──────────────────────────────────────────────────

    async def _requirePending(self, resultId: uuid.UUID) -> AnalysisResult:
        # Loads a result and enforces it's awaiting supervisor action, for
        # approve_result/return_result/escalate_result's shared guard.
        # Raises NotFoundException (RESULT_NOT_FOUND) or ConflictException
        # (INVALID_RESULT_STATUS) if either check fails.
        result = await self.db.get(AnalysisResult, resultId)
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message="Analysis result not found."
            )
        if result.status not in _ALLOWED_STATUSES_FOR_ACTION:
            raise ConflictException(
                code="INVALID_RESULT_STATUS",
                message=f"Action not allowed in status '{result.status}'.",
            )
        return result

    async def _batchPatientContext(
        self, specimenIds: list[uuid.UUID]
    ) -> tuple[dict[uuid.UUID, Specimen], dict[str, Patient], dict[uuid.UUID, str]]:
        """Batch-load specimens, their patients (by patient_uid), and their
        assigned medtechs' usernames, for a page of results.
        """
        if not specimenIds:
            return {}, {}, {}

        specRows = (
            await self.db.execute(select(Specimen).where(Specimen.specimenId.in_(specimenIds)))
        ).scalars().all()
        specMap = {s.specimenId: s for s in specRows}

        patientUids = list({s.patientUid for s in specRows if s.patientUid})
        medtechIds = list({s.medtechId for s in specRows if s.medtechId})

        patMap: dict[str, Patient] = {}
        if patientUids:
            patRows = (
                await self.db.execute(select(Patient).where(Patient.patientUid.in_(patientUids)))
            ).scalars().all()
            patMap = {p.patientUid: p for p in patRows}

        userMap: dict[uuid.UUID, str] = {}
        if medtechIds:
            userRows = (
                await self.db.execute(select(User).where(User.userId.in_(medtechIds)))
            ).scalars().all()
            userMap = {u.userId: u.username for u in userRows}

        return specMap, patMap, userMap

    def _patientDisplay(self, spec: Specimen | None, patMap: dict[str, Patient]) -> tuple[str, int | None, str | None]:
        """Returns (patient_name, patient_age, patient_sex) for a list row."""
        if spec is None:
            return "", None, None
        patientName = _decryptOrNone(spec.patientName) or ""
        pat = patMap.get(spec.patientUid) if spec.patientUid else None
        age = _computeAge(_decryptOrNone(pat.dateOfBirth)) if pat else None
        sex = pat.sex if pat else None
        return patientName, age, sex

    # ── Supervisor dashboard stats ───────────────────────────────────────

    async def getSupervisorStats(self) -> dict[str, int]:
        """Dashboard counts for the supervisor's review queue.

        Returns:
            A dict with `pendingCount` (results awaiting approval),
            `approvedToday` (approvals recorded today, PHT), and
            `escalatedCount` (results currently `CRITICAL_ESCALATED`).
        """
        todayPht = datetime.now(_PHT).date()
        tomorrowPht = todayPht + timedelta(days=1)

        pendingCount = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL)
            )
        ).scalar_one()

        approvedCount = (
            await self.db.execute(
                select(func.count())
                .select_from(ResultApproval)
                .where(
                    ResultApproval.approvedAt >= todayPht,
                    ResultApproval.approvedAt < tomorrowPht,
                )
            )
        ).scalar_one()

        escalatedCount = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
            )
        ).scalar_one()

        return {
            "pendingCount": pendingCount,
            "approvedToday": approvedCount,
            "escalatedCount": escalatedCount,
        }

    # ── Pending queue ─────────────────────────────────────────────────────

    async def getPending(self, page: int, pageSize: int) -> dict[str, Any]:
        """List results awaiting supervisor approval, oldest-confirmed first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech context flattened per row),
            `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * pageSize

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL)
            )
        ).scalar_one()

        stmt = (
            select(AnalysisResult)
            .where(AnalysisResult.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL)
            .order_by(AnalysisResult.confirmedAt.asc())
            .offset(offset)
            .limit(pageSize)
        )
        arRows = (await self.db.execute(stmt)).scalars().all()
        if not arRows:
            return {"items": [], "total": total, "page": page, "pageSize": pageSize}

        specMap, patMap, userMap = await self._batchPatientContext(
            [ar.specimenId for ar in arRows]
        )

        items = []
        for ar in arRows:
            spec = specMap.get(ar.specimenId)
            name, age, sex = self._patientDisplay(spec, patMap)
            items.append(
                {
                    "resultId": ar.resultId,
                    "specimenId": ar.specimenId,
                    "patientName": name,
                    "patientAge": age,
                    "patientSex": sex,
                    "medtechName": userMap.get(spec.medtechId, "") if spec and spec.medtechId else "",
                    "confirmedAt": ar.confirmedAt,
                    "status": ar.status,
                }
            )
        return {"items": items, "total": total, "page": page, "pageSize": pageSize}

    # ── Approved today list ───────────────────────────────────────────────

    async def getApprovedToday(self, page: int, pageSize: int) -> dict[str, Any]:
        """List results approved today (PHT), newest-approved first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech context flattened per row),
            `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * pageSize
        todayPht = datetime.now(_PHT).date()
        tomorrowPht = todayPht + timedelta(days=1)

        window = (ResultApproval.approvedAt >= todayPht, ResultApproval.approvedAt < tomorrowPht)

        total = (
            await self.db.execute(
                select(func.count()).select_from(ResultApproval).where(*window)
            )
        ).scalar_one()

        approvalRows = (
            await self.db.execute(
                select(ResultApproval)
                .where(*window)
                .order_by(ResultApproval.approvedAt.desc())
                .offset(offset)
                .limit(pageSize)
            )
        ).scalars().all()
        if not approvalRows:
            return {"items": [], "total": total, "page": page, "pageSize": pageSize}

        resultIds = [a.resultId for a in approvalRows]
        approvedAtMap = {a.resultId: a.approvedAt for a in approvalRows}

        arRows = (
            await self.db.execute(select(AnalysisResult).where(AnalysisResult.resultId.in_(resultIds)))
        ).scalars().all()
        arMap = {ar.resultId: ar for ar in arRows}

        specMap, patMap, userMap = await self._batchPatientContext(
            [ar.specimenId for ar in arRows]
        )

        items = []
        for resultId in resultIds:
            ar = arMap.get(resultId)
            spec = specMap.get(ar.specimenId) if ar else None
            name, age, sex = self._patientDisplay(spec, patMap)
            items.append(
                {
                    "resultId": resultId,
                    "specimenId": ar.specimenId if ar else None,
                    "patientName": name,
                    "patientAge": age,
                    "patientSex": sex,
                    "medtechName": userMap.get(spec.medtechId, "") if spec and spec.medtechId else "",
                    "approvedAt": approvedAtMap[resultId],
                    "status": ar.status if ar else "APPROVED",
                }
            )
        return {"items": items, "total": total, "page": page, "pageSize": pageSize}

    # ── Escalated list ────────────────────────────────────────────────────

    async def getEscalated(self, page: int, pageSize: int) -> dict[str, Any]:
        """List results currently `CRITICAL_ESCALATED`, newest-updated first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech/escalation context flattened
            per row), `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * pageSize

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
            )
        ).scalar_one()

        arRows = (
            await self.db.execute(
                select(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
                .order_by(AnalysisResult.updatedAt.desc())
                .offset(offset)
                .limit(pageSize)
            )
        ).scalars().all()
        if not arRows:
            return {"items": [], "total": total, "page": page, "pageSize": pageSize}

        resultIds = [ar.resultId for ar in arRows]
        escRows = (
            await self.db.execute(select(Escalation).where(Escalation.resultId.in_(resultIds)))
        ).scalars().all()
        escMap = {e.resultId: e for e in escRows}

        specMap, patMap, userMap = await self._batchPatientContext(
            [ar.specimenId for ar in arRows]
        )

        items = []
        for ar in arRows:
            spec = specMap.get(ar.specimenId)
            name, age, sex = self._patientDisplay(spec, patMap)
            esc = escMap.get(ar.resultId)
            items.append(
                {
                    "resultId": ar.resultId,
                    "specimenId": ar.specimenId,
                    "patientName": name,
                    "patientAge": age,
                    "patientSex": sex,
                    "medtechName": userMap.get(spec.medtechId, "") if spec and spec.medtechId else "",
                    "escalatedAt": esc.escalatedAt if esc else None,
                    "escalationPath": esc.escalationPath if esc else "",
                    "status": ar.status,
                }
            )
        return {"items": items, "total": total, "page": page, "pageSize": pageSize}

    # ── Full result detail ────────────────────────────────────────────────

    async def getFullResult(self, resultId: uuid.UUID) -> dict[str, Any]:
        """Assemble the full supervisor-review detail view for one result:
        patient/medtech context, AI findings, manual overrides, the latest
        annotation, and Smart Diagnosis (if attached).

        Returns:
            A dict of the assembled detail fields. `confirmation_notes` is
            always `None` — see the module docstring's schema-drift note.

        Raises:
            NotFoundException: `result_id` doesn't exist.
        """
        ar = await self.db.get(AnalysisResult, resultId)
        if ar is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message="Analysis result not found."
            )

        spec = await self.db.get(Specimen, ar.specimenId)

        pat: Patient | None = None
        if spec and spec.patientUid:
            pat = (
                await self.db.execute(select(Patient).where(Patient.patientUid == spec.patientUid))
            ).scalar_one_or_none()

        medtechName = ""
        if spec and spec.medtechId:
            medtech = await self.db.get(User, spec.medtechId)
            medtechName = medtech.username if medtech else ""

        imageUrl: str | None = None
        if ar.imageId:
            image = await self.db.get(Image, ar.imageId)
            imageUrl = _imagePublicUrl(image.storageKey) if image else None

        overridesRows = (
            await self.db.execute(
                select(ManualOverride)
                .where(ManualOverride.resultId == resultId)
                .order_by(ManualOverride.overriddenAt)
            )
        ).scalars().all()
        overrides = [
            {
                "overrideId": o.overrideId,
                "parameterName": o.parameterName,
                "originalAiValue": o.originalAiValue,
                "correctedValue": o.correctedValue,
                "rationale": o.rationale,
                "overriddenAt": o.overriddenAt,
            }
            for o in overridesRows
        ]

        review = (
            await self.db.execute(
                select(ResultReview)
                .where(ResultReview.resultId == resultId)
                .order_by(ResultReview.updatedAt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latestAnnotation = review.annotationNotes if review else None
        latestSpatial = review.spatialAnnotations if review else None

        sdo = (
            await self.db.execute(
                select(SmartDiagnosisOutput).where(SmartDiagnosisOutput.resultId == resultId)
            )
        ).scalar_one_or_none()
        smartDiagnosis = None
        if sdo and sdo.status == "ATTACHED":
            smartDiagnosis = {
                "goutScore": sdo.goutScore,
                "gnScore": sdo.gnScore,
                "nephroScore": sdo.nephroScore,
                "noSignificantIndicators": sdo.noSignificantIndicators,
                "evidenceMap": sdo.evidenceMap or {},
                "engineVersion": sdo.engineVersion,
            }

        first = _decryptOrNone(pat.firstName) if pat else None
        last = _decryptOrNone(pat.lastName) if pat else None
        dob = _decryptOrNone(pat.dateOfBirth) if pat else None
        sex = pat.sex if pat else None
        patientName = f"{first or ''} {last or ''}".strip() or (
            _decryptOrNone(spec.patientName) if spec else ""
        ) or ""

        return {
            "resultId": ar.resultId,
            "specimenId": ar.specimenId,
            "patientName": patientName,
            "patientAge": _computeAge(dob),
            "patientSex": sex,
            "medtechName": medtechName,
            "confirmedAt": ar.confirmedAt,
            "confirmationNotes": None,  # schema drift — see module docstring
            "aiFindings": ar.aiFindings or {},
            "flaggedAnomalies": ar.flaggedAnomalies or {},
            "particleClasses": ar.particleClasses or {},
            "modelVersion": ar.modelVersion,
            "manualOverrides": overrides,
            "imageUrl": imageUrl,
            "smartDiagnosisUnavailable": ar.smartDiagnosisUnavailable or smartDiagnosis is None,
            "status": ar.status,
            "annotationNotes": latestAnnotation,
            "spatialAnnotations": latestSpatial,
        }

    # ── Annotation ────────────────────────────────────────────────────────

    async def saveAnnotation(
        self,
        resultId: uuid.UUID,
        userId: uuid.UUID,
        annotationNotes: str,
        spatialAnnotations: list | None = None,
    ) -> dict[str, Any]:
        """Upsert a supervisor's annotation on a result.

        `spatial_annotations` is only written when the caller supplies a
        value (matching the pre-port behavior) — omitting it on a later call
        leaves a previously-saved value in place rather than clearing it.
        """
        ar = await self.db.get(AnalysisResult, resultId)
        if ar is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message="Analysis result not found."
            )

        existing = (
            await self.db.execute(
                select(ResultReview).where(
                    ResultReview.resultId == resultId,
                    ResultReview.reviewedBy == userId,
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.annotationNotes = annotationNotes
            if spatialAnnotations is not None:
                existing.spatialAnnotations = spatialAnnotations
        else:
            self.db.add(
                ResultReview(
                    resultId=resultId,
                    reviewedBy=userId,
                    annotationNotes=annotationNotes,
                    spatialAnnotations=spatialAnnotations,
                )
            )

        await self.db.commit()

        return {
            "resultId": resultId,
            "annotationNotes": annotationNotes,
            "spatialAnnotations": spatialAnnotations,
        }

    # ── Approve ───────────────────────────────────────────────────────────

    async def approveResult(
        self, resultId: uuid.UUID, userId: uuid.UUID, notes: str | None
    ) -> dict[str, Any]:
        """Approve a pending result, marking its specimen `COMPLETED`.

        Args:
            user_id: the authenticated supervisor recorded as `approved_by`.
            notes: optional free-text approval notes.

        Returns:
            A dict confirming the new status and `approved_at` timestamp.

        Raises:
            NotFoundException: `result_id` doesn't exist.
            ConflictException: the result isn't `PENDING_SUPERVISOR_APPROVAL`.
        """
        ar = await self._requirePending(resultId)

        now = datetime.now(_PHT)
        self.db.add(ResultApproval(resultId=resultId, approvedBy=userId, notes=notes))
        ar.status = ResultStatus.APPROVED

        specimen = await self.db.get(Specimen, ar.specimenId)
        if specimen is not None:
            specimen.status = "COMPLETED"
            specimen.completedAt = now

        await self.db.commit()

        return {"resultId": resultId, "status": ResultStatus.APPROVED.value, "approvedAt": now}

    # ── Return for correction ────────────────────────────────────────────

    async def returnResult(
        self, resultId: uuid.UUID, userId: uuid.UUID, reason: str
    ) -> dict[str, Any]:
        """Return a pending result to the MedTech for correction.

        Args:
            user_id: the authenticated supervisor recorded as `returned_by`.
            reason: required free-text explanation for the return.

        Returns:
            A dict confirming the new status and `returned_at` timestamp.

        Raises:
            NotFoundException: `result_id` doesn't exist.
            ConflictException: the result isn't `PENDING_SUPERVISOR_APPROVAL`.
        """
        ar = await self._requirePending(resultId)

        now = datetime.now(_PHT)
        self.db.add(ResultReturn(resultId=resultId, returnedBy=userId, reason=reason))
        ar.status = ResultStatus.RETURNED_FOR_CORRECTION

        await self.db.commit()

        return {
            "resultId": resultId,
            "status": ResultStatus.RETURNED_FOR_CORRECTION.value,
            "returnedAt": now,
        }

    # ── Escalate ──────────────────────────────────────────────────────────

    async def escalateResult(
        self,
        resultId: uuid.UUID,
        userId: uuid.UUID,
        escalationPath: str,
        escalationNote: str | None,
    ) -> dict[str, Any]:
        """Escalate a pending result to `CRITICAL_ESCALATED`.

        Args:
            user_id: the authenticated supervisor recorded as `escalated_by`.
            escalation_path: must be one of `VALID_ESCALATION_PATHS`
                (`NOTIFY_PHYSICIAN`, `FLAG_SENIOR_REVIEW`, `MARK_CRITICAL`).
            escalation_note: optional free-text note.

        Returns:
            A dict confirming the new status, `escalation_path`, and
            `escalated_at` timestamp.

        Raises:
            UnprocessableException: `escalation_path` isn't a valid path.
            NotFoundException: `result_id` doesn't exist.
            ConflictException: the result isn't `PENDING_SUPERVISOR_APPROVAL`.
        """
        if escalationPath not in VALID_ESCALATION_PATHS:
            raise UnprocessableException(
                code="INVALID_ESCALATION_PATH",
                message=f"Invalid escalation_path '{escalationPath}'.",
            )

        ar = await self._requirePending(resultId)

        now = datetime.now(_PHT)
        self.db.add(
            Escalation(
                resultId=resultId,
                escalatedBy=userId,
                escalationPath=escalationPath,
                escalationNote=escalationNote,
            )
        )
        ar.status = ResultStatus.CRITICAL_ESCALATED

        await self.db.commit()

        return {
            "resultId": resultId,
            "status": ResultStatus.CRITICAL_ESCALATED.value,
            "escalationPath": escalationPath,
            "escalatedAt": now,
        }


# ── Smart Diagnosis lookup (plan: neither row 6 nor row 7) ─────────────────────
# Folded in from app/services/result_service.py, unchanged. Still pure
# Supabase REST, not a ResultReviewService method — analysis_results.
# smart_diagnosis and smart_diagnosis_outputs are read here exactly as the
# SmartDiagnosisService writes them, so this stays on the same client rather
# than being ported to SQLAlchemy speculatively.

async def getSmartDiagnosis(resultId: str) -> dict:
    """Fetch a result's Smart Diagnosis output, preferring the authoritative
    `smart_diagnosis_outputs` row and falling back to the denormalized
    `analysis_results.smart_diagnosis` JSONB column if no attached output
    record exists.

    Returns:
        A dict with `status` `"ATTACHED"` (with scores/evidence) if found via
        either source, or `{"result_id": ..., "status": "FLAGGED_UNAVAILABLE"}`
        if neither has usable data.

    Raises:
        HTTPException: 404, if `result_id` doesn't exist.
    """
    # Fetch result including the denormalized smart_diagnosis JSONB column
    result = await (
        supabase.table("analysis_results")
        .select("result_id, smart_diagnosis_unavailable, smart_diagnosis")
        .eq("result_id", resultId)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis result not found.")

    ar = rows[0]

    # Try the smart_diagnosis_outputs table first (authoritative source)
    output = await (
        supabase.table("smart_diagnosis_outputs")
        .select("*")
        .eq("result_id", resultId)
        .execute()
    )
    outputRows = output.data or []

    if outputRows and outputRows[0].get("status") != "FLAGGED_UNAVAILABLE":
        row = outputRows[0]
        evidenceRaw = row.get("evidence_map") or {}
        return {
            "output_id": str(row["output_id"]),
            "result_id": resultId,
            "status": "ATTACHED",
            "gout_score":   row["gout_score"],
            "gn_score":     row["gn_score"],
            "nephro_score": row["nephro_score"],
            "evidence_map": evidenceRaw,
            "no_significant_indicators": row.get("no_significant_indicators", False),
            "engine_version": row.get("engine_version", ""),
            "generated_at": str(row.get("generated_at", "")),
        }

    # Fall back to the denormalized JSONB on analysis_results
    smartDiag = ar.get("smart_diagnosis")
    if smartDiag:
        return {
            "output_id": resultId,
            "result_id": resultId,
            "status": "ATTACHED",
            "gout_score":   smartDiag.get("gout", {}).get("level", "LOW"),
            "gn_score":     smartDiag.get("glomerulonephritis", {}).get("level", "LOW"),
            "nephro_score": smartDiag.get("nephrolithiasis", {}).get("level", "LOW"),
            "evidence_map": {
                "gout":               smartDiag.get("gout", {}),
                "glomerulonephritis": smartDiag.get("glomerulonephritis", {}),
                "nephrolithiasis":    smartDiag.get("nephrolithiasis", {}),
            },
            "no_significant_indicators": smartDiag.get("no_significant_indicators", False),
            "engine_version": smartDiag.get("engine_version", ""),
            "generated_at": "",
        }

    return {"result_id": resultId, "status": "FLAGGED_UNAVAILABLE"}
