# urolens-backend/services/result_return_service.py

"""
Single Responsibility: handles the business logic of a Supervisor returning
a result to the MedTech for correction.  Push notification dispatch is
delegated to NotificationService.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models.analysis_result import AnalysisResult
from models.specimen import Specimen
from models.user import User
from services.audit_logger import AuditLogger
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class ResultReturnService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditLogger,
        notification_service: NotificationService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._notifications = notification_service

    async def return_result(
        self,
        result_id: str,
        supervisor: User,
        return_reason: str,
    ) -> AnalysisResult:
        """
        Mark the result as returned and notify the original MedTech via push.
        """
        result = await self._session.get(AnalysisResult, uuid.UUID(result_id))
        if result is None:
            raise ValueError(f"AnalysisResult {result_id} not found.")

        result.status = "RETURNED_FOR_CORRECTION"
        result.return_reason = return_reason
        self._session.add(result)

        await self._audit.record(
            "RESULT_RETURNED",
            user_id=str(supervisor.id),
            metadata={
                "result_id": result_id,
                "return_reason": return_reason,
            },
        )

        await self._session.commit()

        # Load the MedTech who originally submitted the result.
        medtech: User | None = await self._session.get(User, result.submitted_by_id)

        if medtech and medtech.expo_push_token:
            # Load specimen for the navigation deep-link.
            specimen: Specimen | None = await self._session.get(Specimen, result.specimen_id)
            specimen_id = str(specimen.id) if specimen else str(result.specimen_id)

            await self._notifications.send_result_returned(
                medtech.expo_push_token,
                sample_id=str(result.sample_id),
                specimen_id=specimen_id,
                return_reason=return_reason,
            )
        else:
            logger.info(
                "[ResultReturnService] MedTech has no push token; skipping notification.",
            )

        return result