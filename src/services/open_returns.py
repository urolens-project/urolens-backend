"""Which supervisor returns are still open (multi-field workflow).

A return is open until the MedTech confirms the result again. `result_confirmations`
holds one row per result whose `confirmed_at` moves on every re-confirmation, so a
result's latest return is open exactly when it is newer than that row (or there is
no row). The status cannot say this on its own once a returned result goes back
through capture (`RETURNED_FOR_CORRECTION` → `CAPTURING`).

Used by `ResultFieldService.submitFields` (to send such a result back to
`RETURNED_FOR_CORRECTION`) and `sync_service` (to keep the reason on the phone
while it captures).
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.result_confirmation import ResultConfirmation
from src.models.result_return import ResultReturn


async def openReturnReasons(db: AsyncSession, resultIds: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The reason of each result's open return, keyed by result ID.

    One query. A result with no return, or whose latest return was answered by a
    later confirmation, is absent. Returns newer than the confirmation are taken
    newest first, so a result returned twice gives its latest reason.
    """
    if not resultIds:
        return {}
    rows = (
        await db.execute(
            select(ResultReturn.resultId, ResultReturn.reason)
            .outerjoin(ResultConfirmation, ResultConfirmation.resultId == ResultReturn.resultId)
            .where(
                ResultReturn.resultId.in_(resultIds),
                or_(
                    ResultConfirmation.confirmedAt.is_(None),
                    ResultReturn.returnedAt > ResultConfirmation.confirmedAt,
                ),
            )
            .order_by(ResultReturn.returnedAt.desc())
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for resultId, reason in rows:
        reasons.setdefault(resultId, reason)
    return reasons
