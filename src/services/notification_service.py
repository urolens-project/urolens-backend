"""In-app notification rows plus best-effort Expo push delivery.

A push goes out only after the transaction that created its notification
commits (UROLENS-248): `notify` queues it on the session, and a session
`after_commit` hook sends everything queued, in one request, in the
background. A rollback drops the queue, so a push never announces something
that didn't happen, and no request waits on Expo.
"""
import asyncio
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from ..core.enums import UserRole
from ..models.notification import Notification
from ..models.user import User
from ..schemas.notifications import EXPO_TOKEN_PREFIXES

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

_PUSH_TIMEOUT_SECONDS = 5.0

# Keys in `Session.info`: the messages waiting for the commit, and whether the
# session's commit/rollback hooks are already registered.
_PENDING_PUSHES_KEY = "pendingPushes"
_PUSH_HOOKS_KEY = "pushHooksRegistered"

# Sends in flight. The event loop only keeps weak references to tasks.
_pushTasks: set[asyncio.Task[None]] = set()


class NotificationService:
    """Creates `Notification` rows and attempts best-effort Expo push delivery."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def notify(
        self,
        userId: uuid.UUID,
        message: str,
        notificationType: str,
        entityId: uuid.UUID | None = None,
    ) -> None:
        """Insert a notification row and queue its push for after the commit.

        Never raises. The insert runs in a savepoint, so a failed insert is
        logged and rolled back alone, leaving the caller's transaction usable
        (UROLENS-248). The caller's commit saves the row and sends the push.

        Args:
            entityId: the related entity (e.g. a result or specimen ID), if any.
        """
        try:
            async with self.db.begin_nested():
                stmt = (
                    insert(Notification)
                    .values(
                        user_id=userId,
                        message=message,
                        notification_type=notificationType,
                        entity_id=entityId,
                    )
                    .returning(Notification.notificationId)
                )
                notificationId = (await self.db.execute(stmt)).scalar_one()
        except Exception:
            logger.exception("Failed to create notification for user %s", userId)
            return

        await self._queuePush(userId, notificationId, message, notificationType, entityId)

    async def notifyMedtechResultReturned(
        self, medtechId: uuid.UUID, resultId: uuid.UUID, sampleUid: str
    ) -> None:
        """Tell the MedTech a supervisor returned their result for correction.

        Names the sample only. The supervisor's reason stays out of the message,
        which is pushed to the lock screen; the app gets it through sync
        (UROLENS-225, UROLENS-248).
        """
        await self.notify(
            userId=medtechId,
            message=f"Result for sample {sampleUid} was returned for correction.",
            notificationType="RESULT_RETURNED",
            entityId=resultId,
        )

    async def notifySupervisorResultReady(
        self,
        resultId: uuid.UUID,
        specimenId: uuid.UUID,
    ) -> None:
        """Notify every active supervisor that a result is ready for their review."""
        supervisorIds = await self._getActiveUserIds(UserRole.SUPERVISOR)
        for supId in supervisorIds:
            await self.notify(
                userId=supId,
                message=f"A result is ready for your review (specimen {specimenId}).",
                notificationType="RESULT_READY_FOR_REVIEW",
                entityId=resultId,
            )

    async def notifySupervisorDiagnosisUnavailable(
        self,
        resultId: uuid.UUID,
    ) -> None:
        """Notify every active supervisor that Smart Diagnosis failed for a
        confirmed result.
        """
        supervisorIds = await self._getActiveUserIds(UserRole.SUPERVISOR)
        for supId in supervisorIds:
            await self.notify(
                userId=supId,
                message="Smart Diagnosis is unavailable for a confirmed result due to an engine error.",
                notificationType="SMART_DIAGNOSIS_UNAVAILABLE",
                entityId=resultId,
            )

    async def notifyActiveReceptionists(
        self,
        requestUid: str,
        physicianName: str,
        labRequestId: uuid.UUID,
    ) -> None:
        """Notify every active receptionist that a physician submitted a new
        lab request needing follow-up (e.g. specimen collection).
        """
        # physician_name is the physician's display name/username, which
        # already carries a "Dr." prefix where relevant (e.g. "Dr. Wendell")
        # — don't add a second one.
        receptionistIds = await self._getActiveUserIds(UserRole.RECEPTIONIST)
        for recId in receptionistIds:
            await self.notify(
                userId=recId,
                message=f"New lab request {requestUid} submitted by {physicianName}.",
                notificationType="LAB_REQUEST_SUBMITTED",
                entityId=labRequestId,
            )

    async def notifyReceptionistsSpecimenRejected(
        self, specimenId: uuid.UUID, sampleUid: str, reason: str
    ) -> None:
        """Tell every active receptionist a MedTech rejected an assigned specimen.

        The patient has to give a new specimen, and the web only shows desk
        rejections (UROLENS-238). Identifies the sample by its sample ID — no
        patient details in the message.
        """
        receptionistIds = await self._getActiveUserIds(UserRole.RECEPTIONIST)
        for recId in receptionistIds:
            await self.notify(
                userId=recId,
                message=(
                    f"Specimen {sampleUid} was rejected by the MedTech ({reason}). "
                    "A new specimen needs to be collected from the patient."
                ),
                notificationType="SPECIMEN_REJECTED",
                entityId=specimenId,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _queuePush(
        self,
        userId: uuid.UUID,
        notificationId: uuid.UUID,
        message: str,
        notificationType: str,
        entityId: uuid.UUID | None,
    ) -> None:
        # Holds the push until the session commits (see the module docstring).
        # No-ops if the user has no Expo token, or if the session isn't a real
        # AsyncSession (unit-test mocks), which has no commit hooks.
        if not isinstance(self.db, AsyncSession):
            return
        token = await self._getPushToken(userId)
        if not token or not token.startswith(EXPO_TOKEN_PREFIXES):
            return
        syncSession = self.db.sync_session
        _registerPushHooks(syncSession)
        syncSession.info.setdefault(_PENDING_PUSHES_KEY, []).append(
            {
                "to": token,
                "title": "UroLens",
                "body": message,
                "data": {
                    # Lets the app mark the notification read when the push is tapped.
                    "notification_id": str(notificationId),
                    "notification_type": notificationType,
                    "entity_id": str(entityId) if entityId else None,
                },
                "sound": "default",
            }
        )

    async def _getPushToken(self, userId: uuid.UUID) -> str | None:
        # Returns None (rather than raising) on lookup failure or missing token.
        try:
            stmt = select(User.expoPushToken).where(User.userId == userId)
            result = await self.db.execute(stmt)
            return result.scalar_one_or_none()
        except Exception:
            return None

    async def _getActiveUserIds(self, role: UserRole) -> list[uuid.UUID]:
        # Returns [] (rather than raising) on query failure.
        try:
            stmt = select(User.userId).where(
                User.role == role,
                User.isActive.is_(True),
            )
            rows = await self.db.execute(stmt)
            return list(rows.scalars().all())
        except Exception:
            logger.exception("Failed to query active %s IDs for notification", role)
            return []


# ── Push delivery after commit ────────────────────────────────────────────────

def _registerPushHooks(syncSession: Session) -> None:
    # Once per session; the hooks live and die with it.
    if syncSession.info.get(_PUSH_HOOKS_KEY):
        return
    syncSession.info[_PUSH_HOOKS_KEY] = True
    event.listen(syncSession, "after_commit", _sendPendingPushes)
    event.listen(syncSession, "after_transaction_end", _dropUnsentPushes)


def _sendPendingPushes(syncSession: Session) -> None:
    # Runs synchronously inside the commit, so the send is handed to the event
    # loop rather than awaited — the request never waits on Expo.
    messages = syncSession.info.pop(_PENDING_PUSHES_KEY, None)
    if not messages:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("No event loop to send %d push message(s); dropped", len(messages))
        return
    task = loop.create_task(_deliverPushes(messages))
    _pushTasks.add(task)
    task.add_done_callback(_pushTasks.discard)


def _dropUnsentPushes(syncSession: Session, transaction: SessionTransaction) -> None:
    # The outermost transaction ended without `after_commit` taking the queue:
    # it rolled back, so its notifications don't exist and nothing is sent.
    if transaction.parent is None:
        syncSession.info.pop(_PENDING_PUSHES_KEY, None)


async def _deliverPushes(messages: list[dict[str, Any]]) -> None:
    # One request for the whole batch. Best effort: a failure is logged (count
    # only, never the message or token) and dropped — the in-app row is saved.
    try:
        async with httpx.AsyncClient(timeout=_PUSH_TIMEOUT_SECONDS) as client:
            await client.post(EXPO_PUSH_URL, json=messages)
    except Exception:
        logger.warning("Expo push delivery failed for %d message(s)", len(messages), exc_info=True)
