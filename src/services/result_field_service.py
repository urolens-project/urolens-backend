"""Multi-field service seam: signatures only. Each function raises
`NotImplementedError` until its owner lane fills it in. The foundation creates
this file so both lanes edit different functions of one module; announce any
new function in the PR.

Owners (from the lane split): mobile = acceptField, submitFields; web =
listFields, getField, reopenCapture; shared = discardField (one function that
branches on the result status: CAPTURING is the mobile path, reason optional;
PENDING_CONFIRM or RETURNED_FOR_CORRECTION is the web path, reason required).
Every transition takes the specimen lock via `getAssignedSpecimen`.
"""
from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit_logger import AuditLogger
from ..schemas.field import (
    FieldActionResponse,
    FieldDetail,
    FieldListResponse,
    ResultStatusResponse,
)


class ResultFieldService:
    """Field-level operations for one request."""

    def __init__(self, db: AsyncSession, auditLogger: AuditLogger) -> None:
        """Bind the request-scoped session and audit logger."""
        self.db = db
        self.auditLogger = auditLogger

    async def acceptField(
        self, fieldId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> FieldActionResponse:
        """Owner: mobile. ANALYZED to ACCEPTED, only while the result is CAPTURING."""
        raise NotImplementedError

    async def discardField(
        self, fieldId: uuid.UUID, medtechId: uuid.UUID, reason: str | None, request: Request
    ) -> FieldActionResponse:
        """Owner: mobile and web (shared). Sets the field and its image to DISCARDED."""
        raise NotImplementedError

    async def submitFields(
        self, resultId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> ResultStatusResponse:
        """Owner: mobile. CAPTURING to PENDING_CONFIRM; refuses FIELDS_UNDECIDED or
        FIELDS_BELOW_MINIMUM and never discards a field itself."""
        raise NotImplementedError

    async def listFields(self, resultId: uuid.UUID, claims: dict) -> FieldListResponse:
        """Owner: web. Fields of a result with one signed image URL each; no detections."""
        raise NotImplementedError

    async def getField(
        self, fieldId: uuid.UUID, claims: dict, request: Request
    ) -> FieldDetail:
        """Owner: web. One field in full (detections loaded with `undefer`); records
        the open that `verify` later requires."""
        raise NotImplementedError

    async def reopenCapture(
        self, resultId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> ResultStatusResponse:
        """Owner: web. PENDING_CONFIRM or RETURNED_FOR_CORRECTION back to CAPTURING,
        keeping fields and corrections."""
        raise NotImplementedError
