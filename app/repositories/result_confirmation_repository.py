"""Repository for ResultConfirmation — data-access layer only.

STORY-MOB-09 | EPIC-MOB-06
Repository: urolens-backend
Path: urolens-backend/app/repositories/result_confirmation_repository.py

SOLID:
  - Single Responsibility: only DB I/O for result_confirmations.
  - Dependency Inversion: callers depend on IResultConfirmationRepository.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.result_confirmation import ResultConfirmation


class IResultConfirmationRepository(ABC):
    """Abstract contract for result-confirmation persistence."""

    @abstractmethod
    async def get_by_result_id(
        self, result_id: uuid.UUID
    ) -> ResultConfirmation | None: ...

    @abstractmethod
    async def get_by_id(
        self, confirmation_id: uuid.UUID
    ) -> ResultConfirmation | None: ...

    @abstractmethod
    async def create(self, confirmation: ResultConfirmation) -> ResultConfirmation: ...

    @abstractmethod
    async def save(self, confirmation: ResultConfirmation) -> ResultConfirmation: ...


class ResultConfirmationRepository(IResultConfirmationRepository):
    """SQLAlchemy implementation of IResultConfirmationRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_result_id(
        self, result_id: uuid.UUID
    ) -> ResultConfirmation | None:
        stmt = select(ResultConfirmation).where(
            ResultConfirmation.result_id == result_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(
        self, confirmation_id: uuid.UUID
    ) -> ResultConfirmation | None:
        stmt = select(ResultConfirmation).where(
            ResultConfirmation.id == confirmation_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, confirmation: ResultConfirmation) -> ResultConfirmation:
        self._session.add(confirmation)
        await self._session.flush()
        await self._session.refresh(confirmation)
        return confirmation

    async def save(self, confirmation: ResultConfirmation) -> ResultConfirmation:
        merged = await self._session.merge(confirmation)
        await self._session.flush()
        return merged