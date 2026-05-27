"""Result confirmation service — business logic for STORY-MOB-09.

TASK-MOB-09-3 | STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/app/services/result_confirmation_service.py

SOLID:
  - Single Responsibility: confirmation business rules only.
  - Open/Closed: extend by subclassing, not editing.
  - Dependency Inversion: depends on abstract repository interfaces.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from app.models.analysis_result import AnalysisResult
from app.models.audit_log import AuditLog
from app.models.result_confirmation import ResultConfirmation
from app.repositories.analysis_result_repository import IAnalysisResultRepository
from app.repositories.audit_log_repository import IAuditLogRepository
from app.repositories.result_confirmation_repository import (
    IResultConfirmationRepository,
)
from app.services.smart_diagnosis_service import ISmartDiagnosisService


# ── Custom exceptions (Interface Segregation) ──────────────────────────────────

class ResultNotFoundError(Exception):
    """Raised when the requested AnalysisResult does not exist."""


class ResultAlreadyConfirmedError(Exception):
    """Raised when trying to confirm a result that already has a confirmation."""


class PendingRetakeError(Exception):
    """Raised when a retake is still pending — confirmation is blocked."""


# ── Abstract service contract ──────────────────────────────────────────────────

class IResultConfirmationService(ABC):
    @abstractmethod
    async def confirm_result(
        self,
        result_id: uuid.UUID,
        confirmed_by: uuid.UUID,
        notes: str | None,
    ) -> ResultConfirmation: ...

    @abstractmethod
    async def get_full_result(
        self,
        result_id: uuid.UUID,
    ) -> AnalysisResult: ...


# ── Concrete implementation ────────────────────────────────────────────────────

class ResultConfirmationService(IResultConfirmationService):
    """Handles MedTech result confirmation and Smart Diagnosis trigger."""

    def __init__(
        self,
        result_repo: IAnalysisResultRepository,
        confirmation_repo: IResultConfirmationRepository,
        audit_repo: IAuditLogRepository,
        smart_diagnosis_svc: ISmartDiagnosisService,
    ) -> None:
        self._result_repo = result_repo
        self._confirmation_repo = confirmation_repo
        self._audit_repo = audit_repo
        self._smart_diagnosis_svc = smart_diagnosis_svc

    async def confirm_result(
        self,
        result_id: uuid.UUID,
        confirmed_by: uuid.UUID,
        notes: str | None,
    ) -> ResultConfirmation:
        """Confirm an AI analysis result.

        Business rules
        ──────────────
        1. Result must exist.
        2. Result must not already be confirmed.
        3. No pending image retake may be outstanding.
        4. On success: status → PENDING_SUPERVISOR_APPROVAL, Smart Diagnosis is
           triggered asynchronously, an audit event is written.
        """
        # 1. Existence check
        result = await self._result_repo.get_by_id(result_id)
        if result is None:
            raise ResultNotFoundError(f"AnalysisResult {result_id} not found.")

        # 2. Already confirmed?
        existing = await self._confirmation_repo.get_by_result_id(result_id)
        if existing is not None:
            raise ResultAlreadyConfirmedError(
                f"Result {result_id} has already been confirmed."
            )

        # 3. Pending retake check (status set by image_retake_service)
        if result.status == "PENDING_RETAKE":
            raise PendingRetakeError(
                "Cannot confirm a result while a retake is pending."
            )

        # 4. Create confirmation record
        confirmation = ResultConfirmation(
            result_id=result_id,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(tz=timezone.utc),
            status="PENDING_SUPERVISOR_APPROVAL",
            smart_diagnosis_triggered=False,
            notes=notes,
            is_synced=True,
        )
        confirmation = await self._confirmation_repo.create(confirmation)

        # 5. Update result status
        result.status = "PENDING_SUPERVISOR_APPROVAL"
        await self._result_repo.save(result)

        # 6. Trigger Smart Diagnosis (fire-and-update; errors are non-fatal)
        try:
            await self._smart_diagnosis_svc.trigger(result_id=result_id)
            confirmation.smart_diagnosis_triggered = True
            confirmation = await self._confirmation_repo.save(confirmation)
        except Exception:  # noqa: BLE001
            # Smart Diagnosis failure must never block confirmation
            pass

        # 7. Audit event
        await self._audit_repo.create(
            AuditLog(
                actor_id=confirmed_by,
                action="RESULT_CONFIRMED",
                entity_type="analysis_result",
                entity_id=result_id,
            )
        )

        return confirmation

    async def get_full_result(self, result_id: uuid.UUID) -> AnalysisResult:
        """Return the full AnalysisResult including ai_findings and smart_diagnosis.

        TASK-MOB-09-5: GET /api/v1/results/{id}
        """
        result = await self._result_repo.get_by_id_with_relations(result_id)
        if result is None:
            raise ResultNotFoundError(f"AnalysisResult {result_id} not found.")
        return result