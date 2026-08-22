"""
Supervisor review/approval service — consolidation plan row 7.

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
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException, status

from ..core.config import settings
from ..core.encryption import decrypt_pii
from ..core.exceptions import ConflictException, NotFoundException, UnprocessableException
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


def _compute_age(dob_str: Optional[str]) -> Optional[int]:
    """Age in whole years from an ISO date string, or None if unparseable."""
    if not dob_str:
        return None
    try:
        dob = date.fromisoformat(dob_str[:10])
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except (ValueError, TypeError):
        return None


def _image_public_url(storage_key: Optional[str]) -> Optional[str]:
    # Builds the public Supabase storage URL for a specimen image; returns
    # None if there's no storage key or no configured Supabase URL.
    if not storage_key or not settings.supabase_url:
        return None
    base = settings.supabase_url.rstrip("/")
    return f"{base}/storage/v1/object/public/{settings.supabase_image_bucket}/{storage_key}"


def _decrypt_or_none(ciphertext: Optional[str]) -> Optional[str]:
    # Decrypts PII, returning None (rather than raising) for an unset or
    # undecryptable value.
    if not ciphertext:
        return None
    try:
        return decrypt_pii(ciphertext)
    except Exception:
        return None


class ResultReviewService:
    """Owns the supervisor review/approval workflow: pending queue, approved/
    escalated lists, full result detail, annotation, and the
    approve/return/escalate transitions."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Private helpers ──────────────────────────────────────────────────

    async def _require_pending(self, result_id: uuid.UUID) -> AnalysisResult:
        # Loads a result and enforces it's awaiting supervisor action, for
        # approve_result/return_result/escalate_result's shared guard.
        # Raises NotFoundException (RESULT_NOT_FOUND) or ConflictException
        # (INVALID_RESULT_STATUS) if either check fails.
        result = await self.db.get(AnalysisResult, result_id)
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

    async def _batch_patient_context(
        self, specimen_ids: list[uuid.UUID]
    ) -> tuple[dict[uuid.UUID, Specimen], dict[str, Patient], dict[uuid.UUID, str]]:
        """Batch-load specimens, their patients (by patient_uid), and their
        assigned medtechs' usernames, for a page of results."""
        if not specimen_ids:
            return {}, {}, {}

        spec_rows = (
            await self.db.execute(select(Specimen).where(Specimen.specimen_id.in_(specimen_ids)))
        ).scalars().all()
        spec_map = {s.specimen_id: s for s in spec_rows}

        patient_uids = list({s.patient_uid for s in spec_rows if s.patient_uid})
        medtech_ids = list({s.medtech_id for s in spec_rows if s.medtech_id})

        pat_map: dict[str, Patient] = {}
        if patient_uids:
            pat_rows = (
                await self.db.execute(select(Patient).where(Patient.patient_uid.in_(patient_uids)))
            ).scalars().all()
            pat_map = {p.patient_uid: p for p in pat_rows}

        user_map: dict[uuid.UUID, str] = {}
        if medtech_ids:
            user_rows = (
                await self.db.execute(select(User).where(User.user_id.in_(medtech_ids)))
            ).scalars().all()
            user_map = {u.user_id: u.username for u in user_rows}

        return spec_map, pat_map, user_map

    def _patient_display(self, spec: Optional[Specimen], pat_map: dict[str, Patient]) -> tuple[str, Optional[int], Optional[str]]:
        """Returns (patient_name, patient_age, patient_sex) for a list row."""
        if spec is None:
            return "", None, None
        patient_name = _decrypt_or_none(spec.patient_name) or ""
        pat = pat_map.get(spec.patient_uid) if spec.patient_uid else None
        age = _compute_age(_decrypt_or_none(pat.date_of_birth)) if pat else None
        sex = pat.sex if pat else None
        return patient_name, age, sex

    # ── Supervisor dashboard stats ───────────────────────────────────────

    async def get_supervisor_stats(self) -> dict[str, int]:
        """Dashboard counts for the supervisor's review queue.

        Returns:
            A dict with `pendingCount` (results awaiting approval),
            `approvedToday` (approvals recorded today, PHT), and
            `escalatedCount` (results currently `CRITICAL_ESCALATED`).
        """
        today_pht = datetime.now(_PHT).date()
        tomorrow_pht = today_pht + timedelta(days=1)

        pending_count = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL)
            )
        ).scalar_one()

        approved_count = (
            await self.db.execute(
                select(func.count())
                .select_from(ResultApproval)
                .where(
                    ResultApproval.approved_at >= today_pht,
                    ResultApproval.approved_at < tomorrow_pht,
                )
            )
        ).scalar_one()

        escalated_count = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
            )
        ).scalar_one()

        return {
            "pendingCount": pending_count,
            "approvedToday": approved_count,
            "escalatedCount": escalated_count,
        }

    # ── Pending queue ─────────────────────────────────────────────────────

    async def get_pending(self, page: int, page_size: int) -> dict[str, Any]:
        """List results awaiting supervisor approval, oldest-confirmed first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech context flattened per row),
            `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * page_size

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
            .order_by(AnalysisResult.confirmed_at.asc())
            .offset(offset)
            .limit(page_size)
        )
        ar_rows = (await self.db.execute(stmt)).scalars().all()
        if not ar_rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        spec_map, pat_map, user_map = await self._batch_patient_context(
            [ar.specimen_id for ar in ar_rows]
        )

        items = []
        for ar in ar_rows:
            spec = spec_map.get(ar.specimen_id)
            name, age, sex = self._patient_display(spec, pat_map)
            items.append(
                {
                    "result_id": ar.result_id,
                    "specimen_id": ar.specimen_id,
                    "patient_name": name,
                    "patient_age": age,
                    "patient_sex": sex,
                    "medtech_name": user_map.get(spec.medtech_id, "") if spec and spec.medtech_id else "",
                    "confirmed_at": ar.confirmed_at,
                    "status": ar.status,
                }
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    # ── Approved today list ───────────────────────────────────────────────

    async def get_approved_today(self, page: int, page_size: int) -> dict[str, Any]:
        """List results approved today (PHT), newest-approved first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech context flattened per row),
            `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * page_size
        today_pht = datetime.now(_PHT).date()
        tomorrow_pht = today_pht + timedelta(days=1)

        window = (ResultApproval.approved_at >= today_pht, ResultApproval.approved_at < tomorrow_pht)

        total = (
            await self.db.execute(
                select(func.count()).select_from(ResultApproval).where(*window)
            )
        ).scalar_one()

        approval_rows = (
            await self.db.execute(
                select(ResultApproval)
                .where(*window)
                .order_by(ResultApproval.approved_at.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).scalars().all()
        if not approval_rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        result_ids = [a.result_id for a in approval_rows]
        approved_at_map = {a.result_id: a.approved_at for a in approval_rows}

        ar_rows = (
            await self.db.execute(select(AnalysisResult).where(AnalysisResult.result_id.in_(result_ids)))
        ).scalars().all()
        ar_map = {ar.result_id: ar for ar in ar_rows}

        spec_map, pat_map, user_map = await self._batch_patient_context(
            [ar.specimen_id for ar in ar_rows]
        )

        items = []
        for result_id in result_ids:
            ar = ar_map.get(result_id)
            spec = spec_map.get(ar.specimen_id) if ar else None
            name, age, sex = self._patient_display(spec, pat_map)
            items.append(
                {
                    "result_id": result_id,
                    "specimen_id": ar.specimen_id if ar else None,
                    "patient_name": name,
                    "patient_age": age,
                    "patient_sex": sex,
                    "medtech_name": user_map.get(spec.medtech_id, "") if spec and spec.medtech_id else "",
                    "approved_at": approved_at_map[result_id],
                    "status": ar.status if ar else "APPROVED",
                }
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    # ── Escalated list ────────────────────────────────────────────────────

    async def get_escalated(self, page: int, page_size: int) -> dict[str, Any]:
        """List results currently `CRITICAL_ESCALATED`, newest-updated first.

        Args:
            page: 1-indexed page number.
            page_size: rows per page.

        Returns:
            A dict with `items` (patient/medtech/escalation context flattened
            per row), `total`, `page`, and `page_size`.
        """
        offset = (page - 1) * page_size

        total = (
            await self.db.execute(
                select(func.count())
                .select_from(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
            )
        ).scalar_one()

        ar_rows = (
            await self.db.execute(
                select(AnalysisResult)
                .where(AnalysisResult.status == ResultStatus.CRITICAL_ESCALATED)
                .order_by(AnalysisResult.updated_at.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).scalars().all()
        if not ar_rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        result_ids = [ar.result_id for ar in ar_rows]
        esc_rows = (
            await self.db.execute(select(Escalation).where(Escalation.result_id.in_(result_ids)))
        ).scalars().all()
        esc_map = {e.result_id: e for e in esc_rows}

        spec_map, pat_map, user_map = await self._batch_patient_context(
            [ar.specimen_id for ar in ar_rows]
        )

        items = []
        for ar in ar_rows:
            spec = spec_map.get(ar.specimen_id)
            name, age, sex = self._patient_display(spec, pat_map)
            esc = esc_map.get(ar.result_id)
            items.append(
                {
                    "result_id": ar.result_id,
                    "specimen_id": ar.specimen_id,
                    "patient_name": name,
                    "patient_age": age,
                    "patient_sex": sex,
                    "medtech_name": user_map.get(spec.medtech_id, "") if spec and spec.medtech_id else "",
                    "escalated_at": esc.escalated_at if esc else None,
                    "escalation_path": esc.escalation_path if esc else "",
                    "status": ar.status,
                }
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    # ── Full result detail ────────────────────────────────────────────────

    async def get_full_result(self, result_id: uuid.UUID) -> dict[str, Any]:
        """Assemble the full supervisor-review detail view for one result:
        patient/medtech context, AI findings, manual overrides, the latest
        annotation, and Smart Diagnosis (if attached).

        Returns:
            A dict of the assembled detail fields. `confirmation_notes` is
            always `None` — see the module docstring's schema-drift note.

        Raises:
            NotFoundException: `result_id` doesn't exist.
        """
        ar = await self.db.get(AnalysisResult, result_id)
        if ar is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message="Analysis result not found."
            )

        spec = await self.db.get(Specimen, ar.specimen_id)

        pat: Optional[Patient] = None
        if spec and spec.patient_uid:
            pat = (
                await self.db.execute(select(Patient).where(Patient.patient_uid == spec.patient_uid))
            ).scalar_one_or_none()

        medtech_name = ""
        if spec and spec.medtech_id:
            medtech = await self.db.get(User, spec.medtech_id)
            medtech_name = medtech.username if medtech else ""

        image_url: Optional[str] = None
        if ar.image_id:
            image = await self.db.get(Image, ar.image_id)
            image_url = _image_public_url(image.storage_key) if image else None

        overrides_rows = (
            await self.db.execute(
                select(ManualOverride).where(ManualOverride.result_id == result_id)
            )
        ).scalars().all()
        overrides = [
            {
                "override_id": o.override_id,
                "parameter_name": o.parameter_name,
                "original_ai_value": o.original_ai_value,
                "corrected_value": o.corrected_value,
                "rationale": o.rationale,
                "overridden_at": o.overridden_at,
            }
            for o in overrides_rows
        ]

        review = (
            await self.db.execute(
                select(ResultReview)
                .where(ResultReview.result_id == result_id)
                .order_by(ResultReview.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latest_annotation = review.annotation_notes if review else None
        latest_spatial = review.spatial_annotations if review else None

        sdo = (
            await self.db.execute(
                select(SmartDiagnosisOutput).where(SmartDiagnosisOutput.result_id == result_id)
            )
        ).scalar_one_or_none()
        smart_diagnosis = None
        if sdo and sdo.status == "ATTACHED":
            smart_diagnosis = {
                "gout_score": sdo.gout_score,
                "gn_score": sdo.gn_score,
                "nephro_score": sdo.nephro_score,
                "no_significant_indicators": sdo.no_significant_indicators,
                "evidence_map": sdo.evidence_map or {},
                "engine_version": sdo.engine_version,
            }

        first = _decrypt_or_none(pat.first_name) if pat else None
        last = _decrypt_or_none(pat.last_name) if pat else None
        dob = _decrypt_or_none(pat.date_of_birth) if pat else None
        sex = pat.sex if pat else None
        patient_name = f"{first or ''} {last or ''}".strip() or (
            _decrypt_or_none(spec.patient_name) if spec else ""
        ) or ""

        return {
            "result_id": ar.result_id,
            "specimen_id": ar.specimen_id,
            "patient_name": patient_name,
            "patient_age": _compute_age(dob),
            "patient_sex": sex,
            "medtech_name": medtech_name,
            "confirmed_at": ar.confirmed_at,
            "confirmation_notes": None,  # schema drift — see module docstring
            "ai_findings": ar.ai_findings or {},
            "flagged_anomalies": ar.flagged_anomalies or {},
            "particle_classes": ar.particle_classes or {},
            "model_version": ar.model_version,
            "manual_overrides": overrides,
            "image_url": image_url,
            "smart_diagnosis_unavailable": ar.smart_diagnosis_unavailable or smart_diagnosis is None,
            "status": ar.status,
            "annotation_notes": latest_annotation,
            "spatial_annotations": latest_spatial,
        }

    # ── Annotation ────────────────────────────────────────────────────────

    async def save_annotation(
        self,
        result_id: uuid.UUID,
        user_id: uuid.UUID,
        annotation_notes: str,
        spatial_annotations: Optional[list] = None,
    ) -> dict[str, Any]:
        """Upsert a supervisor's annotation on a result.

        `spatial_annotations` is only written when the caller supplies a
        value (matching the pre-port behavior) — omitting it on a later call
        leaves a previously-saved value in place rather than clearing it.
        """
        ar = await self.db.get(AnalysisResult, result_id)
        if ar is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message="Analysis result not found."
            )

        existing = (
            await self.db.execute(
                select(ResultReview).where(
                    ResultReview.result_id == result_id,
                    ResultReview.reviewed_by == user_id,
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.annotation_notes = annotation_notes
            if spatial_annotations is not None:
                existing.spatial_annotations = spatial_annotations
        else:
            self.db.add(
                ResultReview(
                    result_id=result_id,
                    reviewed_by=user_id,
                    annotation_notes=annotation_notes,
                    spatial_annotations=spatial_annotations,
                )
            )

        await self.db.commit()

        return {
            "result_id": result_id,
            "annotation_notes": annotation_notes,
            "spatial_annotations": spatial_annotations,
        }

    # ── Approve ───────────────────────────────────────────────────────────

    async def approve_result(
        self, result_id: uuid.UUID, user_id: uuid.UUID, notes: Optional[str]
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
        ar = await self._require_pending(result_id)

        now = datetime.now(_PHT)
        self.db.add(ResultApproval(result_id=result_id, approved_by=user_id, notes=notes))
        ar.status = ResultStatus.APPROVED

        specimen = await self.db.get(Specimen, ar.specimen_id)
        if specimen is not None:
            specimen.status = "COMPLETED"
            specimen.completed_at = now

        await self.db.commit()

        return {"result_id": result_id, "status": ResultStatus.APPROVED.value, "approved_at": now}

    # ── Return for correction ────────────────────────────────────────────

    async def return_result(
        self, result_id: uuid.UUID, user_id: uuid.UUID, reason: str
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
        ar = await self._require_pending(result_id)

        now = datetime.now(_PHT)
        self.db.add(ResultReturn(result_id=result_id, returned_by=user_id, reason=reason))
        ar.status = ResultStatus.RETURNED_FOR_CORRECTION

        await self.db.commit()

        return {
            "result_id": result_id,
            "status": ResultStatus.RETURNED_FOR_CORRECTION.value,
            "returned_at": now,
        }

    # ── Escalate ──────────────────────────────────────────────────────────

    async def escalate_result(
        self,
        result_id: uuid.UUID,
        user_id: uuid.UUID,
        escalation_path: str,
        escalation_note: Optional[str],
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
        if escalation_path not in VALID_ESCALATION_PATHS:
            raise UnprocessableException(
                code="INVALID_ESCALATION_PATH",
                message=f"Invalid escalation_path '{escalation_path}'.",
            )

        ar = await self._require_pending(result_id)

        now = datetime.now(_PHT)
        self.db.add(
            Escalation(
                result_id=result_id,
                escalated_by=user_id,
                escalation_path=escalation_path,
                escalation_note=escalation_note,
            )
        )
        ar.status = ResultStatus.CRITICAL_ESCALATED

        await self.db.commit()

        return {
            "result_id": result_id,
            "status": ResultStatus.CRITICAL_ESCALATED.value,
            "escalation_path": escalation_path,
            "escalated_at": now,
        }


# ── Smart Diagnosis lookup (plan: neither row 6 nor row 7) ─────────────────────
# Folded in from app/services/result_service.py, unchanged. Still pure
# Supabase REST, not a ResultReviewService method — analysis_results.
# smart_diagnosis and smart_diagnosis_outputs are read here exactly as the
# SmartDiagnosisService writes them, so this stays on the same client rather
# than being ported to SQLAlchemy speculatively.

async def get_smart_diagnosis(result_id: str) -> dict:
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
        .eq("result_id", result_id)
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
        .eq("result_id", result_id)
        .execute()
    )
    output_rows = output.data or []

    if output_rows and output_rows[0].get("status") != "FLAGGED_UNAVAILABLE":
        row = output_rows[0]
        evidence_raw = row.get("evidence_map") or {}
        return {
            "output_id": str(row["output_id"]),
            "result_id": result_id,
            "status": "ATTACHED",
            "gout_score":   row["gout_score"],
            "gn_score":     row["gn_score"],
            "nephro_score": row["nephro_score"],
            "evidence_map": evidence_raw,
            "no_significant_indicators": row.get("no_significant_indicators", False),
            "engine_version": row.get("engine_version", ""),
            "generated_at": str(row.get("generated_at", "")),
        }

    # Fall back to the denormalized JSONB on analysis_results
    smart_diag = ar.get("smart_diagnosis")
    if smart_diag:
        return {
            "output_id": result_id,
            "result_id": result_id,
            "status": "ATTACHED",
            "gout_score":   smart_diag.get("gout", {}).get("level", "LOW"),
            "gn_score":     smart_diag.get("glomerulonephritis", {}).get("level", "LOW"),
            "nephro_score": smart_diag.get("nephrolithiasis", {}).get("level", "LOW"),
            "evidence_map": {
                "gout":               smart_diag.get("gout", {}),
                "glomerulonephritis": smart_diag.get("glomerulonephritis", {}),
                "nephrolithiasis":    smart_diag.get("nephrolithiasis", {}),
            },
            "no_significant_indicators": smart_diag.get("no_significant_indicators", False),
            "engine_version": smart_diag.get("engine_version", ""),
            "generated_at": "",
        }

    return {"result_id": result_id, "status": "FLAGGED_UNAVAILABLE"}
