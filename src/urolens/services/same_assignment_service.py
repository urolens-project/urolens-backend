# urolens-backend/services/sample_assignment_service.py

"""
Single Responsibility: this service handles the business logic of assigning
a sample to a MedTech.  Push notification dispatch is delegated to
NotificationService (Dependency Inversion — injected, not instantiated here).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from models.sample import Sample
from models.user import User
from services.audit_logger import AuditLogger
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class SampleAssignmentService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditLogger,
        notification_service: NotificationService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._notifications = notification_service

    async def assign_sample(
        self,
        sample_id: str,
        medtech: User,
        assigned_by_id: str,
    ) -> Sample:
        """
        Assign `sample_id` to `medtech` and notify them via push.
        The notification is fire-and-forget — failure never rolls back the assignment.
        """
        sample = await self._session.get(Sample, uuid.UUID(sample_id))
        if sample is None:
            raise ValueError(f"Sample {sample_id} not found.")

        sample.assigned_medtech_id = medtech.id
        self._session.add(sample)

        await self._audit.record(
            "SAMPLE_ASSIGNED",
            user_id=assigned_by_id,
            metadata={"sample_id": sample_id, "medtech_id": str(medtech.id)},
        )

        await self._session.commit()

        # Fire push notification if the MedTech has a registered token.
        if medtech.expo_push_token:
            await self._notifications.send_sample_assigned(
                medtech.expo_push_token,
                sample_id=sample_id,
            )
        else:
            logger.info(
                "[SampleAssignmentService] MedTech %s has no push token; skipping notification.",
                medtech.id,
            )

        return sample