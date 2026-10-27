"""Multi-field service seam.

Web lane (implemented): `listFields`, `getField`. Everything else is still a
signature that raises `NotImplementedError` until its owner lane fills it in. The
foundation created this file so both lanes edit different functions of one
module; announce any new function in the PR.

Owners (from the lane split): mobile = acceptField, submitFields; web =
listFields, getField, reopenCapture; shared = discardField (one function that
branches on the result status: CAPTURING is the mobile path, reason optional;
PENDING_CONFIRM or RETURNED_FOR_CORRECTION is the web path, reason required).
Every transition takes the specimen lock via `getAssignedSpecimen`.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ..core.audit_logger import AuditLogger
from ..core.config import settings
from ..core.exceptions import NotFoundException, UnprocessableException
from ..core.particle_counts import toWholeCount
from ..core.rule_thresholds import loadNormalMax
from ..core.storage import signedImageUrls
from ..models.analysis_result import AnalysisResult, ResultStatus
from ..models.field_annotation_edit import EditAction, FieldAnnotationEdit
from ..models.image import Image, ImageStatus
from ..models.manual_override import ManualOverride, OverrideKind
from ..models.result_field import FieldStatus, ResultField
from ..schemas.field import (
    AddedBox,
    Detection,
    FieldActionResponse,
    FieldAnnotationEdits,
    FieldDetail,
    FieldFlag,
    FieldListResponse,
    FieldOverride,
    FieldSummaryItem,
    FocalFinding,
    ParticleAggregate,
    ResultStatusResponse,
    SummaryCorrection,
    VerificationSummaryResponse,
)
from .field_access import (
    CONFIRMABLE_RESULT_STATUSES,
    LEGACY_CONFIRMABLE_RESULT_STATUSES,
    fieldNotFound,
    loadFieldForWrite,
    readFieldAccess,
    requireVerificationPhase,
)
from .field_aggregation_service import (
    AcceptedField,
    FieldCounts,
    FlagThresholds,
    aggregate,
    classMeans,
    displayValue,
    exactMeans,
    fieldBlockingReason,
    flagsFor,
)
from .notification_service import NotificationService
from .smart_diagnosis_service import SmartDiagnosisService
from .specimen_access import (
    MEDTECH_EDITABLE_RESULT_STATUSES,
    getAssignedSpecimen,
    isMedtech,
    requireSpecimenNotRejected,
)


def flagThresholds() -> FlagThresholds:
    """Flag thresholds from settings, plus the rules-config limits (empty if unreadable)."""
    return FlagThresholds(
        lowConfidence=settings.fieldLowConfidenceThreshold,
        outlierMultiplier=settings.fieldOutlierMultiplier,
        outlierMinAbove=settings.fieldOutlierMinAbove,
        normalMax=loadNormalMax(),
    )


def fieldCountsOf(field: ResultField) -> FieldCounts:
    """The numbers the flags need, from a field row (never touches `detections`)."""
    return FieldCounts(
        aiFindings=field.aiFindings or {},
        effectiveCounts=field.effectiveCounts or {},
        confidenceScores=field.confidenceScores or {},
    )


def acceptedMeans(fields: Sequence[ResultField]) -> dict[str, float]:
    """Exact mean effective count per class over the ACCEPTED fields in `fields`."""
    return classMeans([f.effectiveCounts or {} for f in fields if f.status == FieldStatus.ACCEPTED])


def flagModels(field: ResultField, means: dict[str, float], thresholds: FlagThresholds) -> list[FieldFlag]:
    if field.status == FieldStatus.DISCARDED:
        return []
    return [
        FieldFlag(reason=reason, particleType=particleType)  # type: ignore[arg-type]
        for reason, particleType in flagsFor(fieldCountsOf(field), means, thresholds)
    ]


def activeOverrideRows(rows: Sequence[ManualOverride]) -> dict[str, ManualOverride]:
    # Latest row per class decides: an OVERRIDE is in force, a REVERT ends it.
    active: dict[str, ManualOverride] = {}
    for row in sorted(rows, key=lambda r: r.overriddenAt):
        if row.kind == OverrideKind.REVERT:
            active.pop(row.parameterName, None)
        else:
            active[row.parameterName] = row
    return active


def summaryItem(
    field: ResultField,
    imageUrl: str | None,
    flags: list[FieldFlag],
    hasEdits: bool,
    activeOverrides: dict[str, ManualOverride],
) -> FieldSummaryItem:
    return FieldSummaryItem(
        fieldId=field.fieldId,
        resultId=field.resultId,
        fieldNo=field.fieldNo,
        magnification=field.magnification,  # type: ignore[arg-type]
        status=field.status,  # type: ignore[arg-type]
        imageUrl=imageUrl,
        aiFindings=field.aiFindings or {},
        effectiveCounts=field.effectiveCounts or {},
        confidenceScores=field.confidenceScores or {},
        flags=flags,
        hasCorrections=hasEdits or bool(activeOverrides),
        overriddenClasses=sorted(activeOverrides),
        verifiedBy=field.verifiedBy,
        verifiedAt=field.verifiedAt,
        discardReason=field.discardReason,
        version=field.version,
        createdAt=field.createdAt,
        updatedAt=field.updatedAt,
    )


async def loadAcceptedFields(db: AsyncSession, resultId: uuid.UUID) -> list[ResultField]:
    """The result's ACCEPTED fields in capture order (detections are not loaded)."""
    return list(
        (
            await db.execute(
                select(ResultField)
                .where(ResultField.resultId == resultId, ResultField.status == FieldStatus.ACCEPTED)
                .order_by(ResultField.fieldNo)
            )
        ).scalars().all()
    )


def legacyCounts(aiFindings: dict, resultLevelOverrides: Sequence[ManualOverride]) -> dict[str, float]:
    """Counts of a single-image (legacy) result: the AI findings with its result-level overrides on top.

    The same settling confirm has always done; override rows are oldest first so the
    latest per class wins. A row with a non-numeric value is skipped.
    """
    counts: dict[str, float] = dict(aiFindings or {})
    for row in resultLevelOverrides:
        if row.fieldId is not None or row.kind == OverrideKind.REVERT:
            continue
        try:
            counts[row.parameterName] = float(row.correctedValue)
        except (TypeError, ValueError):
            continue
    return counts


async def buildFieldActionResponse(db: AsyncSession, fieldId: uuid.UUID) -> FieldActionResponse:
    """The response every field action returns: the field plus the running totals.

    Reads the result's fields from the database (a query flushes pending changes
    first, so the response reflects them), without detections: three queries and
    one image-signing call. Flags are computed against the accepted-field means.
    """
    resultId = (
        await db.execute(select(ResultField.resultId).where(ResultField.fieldId == fieldId))
    ).scalar_one_or_none()
    if resultId is None:
        raise fieldNotFound(fieldId)
    rows = (
        await db.execute(
            select(ResultField, Image.storageKey)
            .join(Image, Image.imageId == ResultField.imageId)
            .where(ResultField.resultId == resultId)
            .order_by(ResultField.fieldNo)
        )
    ).all()
    field, storageKey = next((f, key) for f, key in rows if f.fieldId == fieldId)
    hasEdits = bool(
        (
            await db.execute(
                select(FieldAnnotationEdit.editId)
                .where(
                    FieldAnnotationEdit.fieldId == fieldId,
                    FieldAnnotationEdit.supersededAt.is_(None),
                )
                .limit(1)
            )
        ).first()
    )
    overrideRows = (
        await db.execute(
            select(ManualOverride)
            .where(ManualOverride.fieldId == fieldId)
            .order_by(ManualOverride.overriddenAt)
        )
    ).scalars().all()
    urls = await signedImageUrls([storageKey])
    fields = [f for f, _ in rows]
    item = summaryItem(
        field,
        urls.get(storageKey) if storageKey else None,
        flagModels(field, acceptedMeans(fields), flagThresholds()),
        hasEdits,
        activeOverrideRows(overrideRows),
    )
    return FieldActionResponse(
        field=item,
        acceptedFieldCount=sum(f.status == FieldStatus.ACCEPTED for f in fields),
        verifiedFieldCount=sum(
            f.status == FieldStatus.ACCEPTED and f.verifiedBy is not None for f in fields
        ),
        undecidedFieldCount=sum(f.status == FieldStatus.ANALYZED for f in fields),
    )


class ResultFieldService:
    """Field-level operations for one request."""

    def __init__(
        self,
        db: AsyncSession,
        auditLogger: AuditLogger,
        _notifService: NotificationService | None = None,
    ) -> None:
        """Bind the request-scoped session, audit logger and notifier."""
        self.db = db
        self.auditLogger = auditLogger
        self._notifService = _notifService or NotificationService(db=db)

    async def acceptField(
        self, fieldId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> FieldActionResponse:
        """Owner: mobile. ANALYZED to ACCEPTED, only while the result is CAPTURING."""
        raise NotImplementedError

    async def discardField(
        self, fieldId: uuid.UUID, medtechId: uuid.UUID, reason: str | None, request: Request
    ) -> FieldActionResponse:
        """Owner: mobile and web (shared). Sets the field and its image to DISCARDED.

        One function, two paths by the result's status. **Web path (implemented):**
        `PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`, reason required. The field and
        its image become `DISCARDED`, the field's `version` goes up, and the result's
        `field_count` (ACCEPTED fields) and first-accepted-field image pointer are
        updated. Discarding is not a correction: verified marks are untouched.
        Already discarded: returns the current state. **Mobile path (`CAPTURING`):
        owned by the mobile lane, still `NotImplementedError`.**

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`, for any
                status other than the web and mobile paths above.
            UnprocessableException: `REASON_REQUIRED`, if the reason is missing or blank.
        """
        field, result, _specimen = await loadFieldForWrite(self.db, fieldId, medtechId)
        if result.status == ResultStatus.CAPTURING:
            raise NotImplementedError  # mobile path: CAPTURING, reason optional
        requireVerificationPhase(result)
        if field.status == FieldStatus.DISCARDED:
            return await buildFieldActionResponse(self.db, fieldId)
        if reason is None or not reason.strip():
            raise UnprocessableException(
                code="REASON_REQUIRED", message="A reason is required to exclude a field."
            )

        now = datetime.now(UTC)
        field.status = FieldStatus.DISCARDED
        field.discardReason = reason.strip()
        field.version += 1
        image = await self.db.get(Image, field.imageId)
        if image is not None:
            image.status = ImageStatus.DISCARDED
            image.discardedAt = now
        await self.db.flush()

        accepted = (
            await self.db.execute(
                select(ResultField.imageId)
                .where(
                    ResultField.resultId == field.resultId,
                    ResultField.status == FieldStatus.ACCEPTED,
                )
                .order_by(ResultField.fieldNo)
            )
        ).scalars().all()
        result.fieldCount = len(accepted)
        if accepted:
            result.imageId = accepted[0]

        response = await buildFieldActionResponse(self.db, fieldId)
        await self.auditLogger.record(
            eventType="FIELD_DISCARDED",
            entityType="analysis_result",
            entityId=field.resultId,
            userId=medtechId,
            detailJson={"specimen_id": str(field.specimenId), "field_id": str(fieldId)},
            db=self.db,
            request=request,
        )
        await self.db.commit()
        return response

    async def submitFields(
        self, resultId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> ResultStatusResponse:
        """Owner: mobile. CAPTURING to PENDING_CONFIRM; refuses FIELDS_UNDECIDED or
        FIELDS_BELOW_MINIMUM and never discards a field itself."""
        raise NotImplementedError

    async def listFields(self, resultId: uuid.UUID, claims: dict) -> FieldListResponse:
        """Owner: web. Fields of a result with one signed image URL each; no detections.

        A MedTech sees only an assigned specimen, a Supervisor any result. Every
        field is returned (all statuses), in capture order. Flags are computed
        against the means of the ACCEPTED fields and are never stored. One query
        each for the fields, the edits and the overrides, plus one batched sign.

        Raises:
            NotFoundException: `RESULT_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, for a MedTech on another's specimen.
        """
        result = await self.db.get(AnalysisResult, resultId)
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message=f"No analysis result found with id {resultId}."
            )
        await readFieldAccess(
            self.db, result.specimenId, uuid.UUID(claims["user_id"]), claims["role"]
        )

        rows = (
            await self.db.execute(
                select(ResultField, Image.storageKey)
                .join(Image, Image.imageId == ResultField.imageId)
                .where(ResultField.resultId == resultId)
                .order_by(ResultField.fieldNo)
            )
        ).all()
        fields = [field for field, _ in rows]
        fieldIds = [field.fieldId for field in fields]

        urls = await signedImageUrls([key for _, key in rows])
        editedIds = await self._fieldIdsWithCurrentEdits(fieldIds)
        overridesByField = await self._overrideRowsByField(fieldIds)

        means = acceptedMeans(fields)
        thresholds = flagThresholds()
        items = [
            summaryItem(
                field,
                urls.get(key) if key else None,
                flagModels(field, means, thresholds),
                field.fieldId in editedIds,
                activeOverrideRows(overridesByField.get(field.fieldId, [])),
            )
            for field, key in rows
        ]
        return FieldListResponse(
            resultId=resultId,
            resultStatus=result.status,  # type: ignore[arg-type]
            minFields=settings.minFieldsPerSpecimen,
            acceptedFieldCount=sum(f.status == FieldStatus.ACCEPTED for f in fields),
            verifiedFieldCount=sum(
                f.status == FieldStatus.ACCEPTED and f.verifiedBy is not None for f in fields
            ),
            undecidedFieldCount=sum(f.status == FieldStatus.ANALYZED for f in fields),
            items=items,
        )

    async def getField(
        self, fieldId: uuid.UUID, claims: dict, request: Request
    ) -> FieldDetail:
        """Owner: web. One field in full (detections loaded with `undefer`).

        Returns the AI detections, only the current (not superseded) box edits, and
        the overrides in force. For a MedTech, when the result awaits verification
        (`PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`), it also records the open
        that `verify` later requires (`last_opened_by`, `last_opened_at`). Nothing
        is recorded while the result is `CAPTURING` (the phone refilling boxes) or
        for a Supervisor. The open does not touch `result_fields.updated_at`, which
        drives mobile delta sync, and writes no audit row.

        Raises:
            NotFoundException: `FIELD_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, for a MedTech on another's specimen.
        """
        field = (
            await self.db.execute(
                select(ResultField)
                .options(undefer(ResultField.detections))
                .where(ResultField.fieldId == fieldId)
            )
        ).scalar_one_or_none()
        if field is None:
            raise fieldNotFound(fieldId)

        viewerId = uuid.UUID(claims["user_id"])
        result = await self.db.get(AnalysisResult, field.resultId)
        await readFieldAccess(self.db, field.specimenId, viewerId, claims["role"])

        recordOpen = False
        if isMedtech(claims["role"]) and result is not None and result.status in MEDTECH_EDITABLE_RESULT_STATUSES:
            # Lock the specimen, then look at the status again: a concurrent return,
            # reopen or confirm may have moved the result since the first read.
            await getAssignedSpecimen(self.db, field.specimenId, viewerId)
            currentStatus = (
                await self.db.execute(
                    select(AnalysisResult.status).where(AnalysisResult.resultId == field.resultId)
                )
            ).scalar_one()
            recordOpen = currentStatus in MEDTECH_EDITABLE_RESULT_STATUSES

        storageKey = (
            await self.db.execute(select(Image.storageKey).where(Image.imageId == field.imageId))
        ).scalar_one_or_none()
        urls = await signedImageUrls([storageKey])
        edits = (
            await self.db.execute(
                select(FieldAnnotationEdit)
                .where(
                    FieldAnnotationEdit.fieldId == fieldId,
                    FieldAnnotationEdit.supersededAt.is_(None),
                )
                .order_by(FieldAnnotationEdit.editedAt)
            )
        ).scalars().all()
        overrideRows = (await self._overrideRowsByField([fieldId])).get(fieldId, [])
        active = activeOverrideRows(overrideRows)
        accepted = (
            await self.db.execute(
                select(ResultField).where(
                    ResultField.resultId == field.resultId,
                    ResultField.status == FieldStatus.ACCEPTED,
                )
            )
        ).scalars().all()

        item = summaryItem(
            field,
            urls.get(storageKey) if storageKey else None,
            flagModels(field, acceptedMeans(accepted), flagThresholds()),
            bool(edits),
            active,
        )
        detail = FieldDetail(
            **item.model_dump(),
            detections=[Detection(**d) for d in (field.detections or [])],
            imageWidth=field.imageWidth,
            imageHeight=field.imageHeight,
            annotationEdits=FieldAnnotationEdits(
                removedDetectionIds=[
                    e.detectionId for e in edits
                    if e.action == EditAction.REMOVED and e.detectionId is not None
                ],
                addedBoxes=[
                    AddedBox(
                        id=str(e.editId), particleType=e.particleType,
                        x=e.x, y=e.y, w=e.w, h=e.h,  # type: ignore[arg-type]
                    )
                    for e in edits if e.action == EditAction.ADDED
                ],
                editedBy=edits[-1].editedBy if edits else None,
                editedAt=edits[-1].editedAt if edits else None,
            ),
            overrides=[
                FieldOverride(
                    particleType=row.parameterName,
                    originalAiValue=toWholeCount(row.originalAiValue),
                    correctedValue=toWholeCount(row.correctedValue),
                    rationale=row.rationale,
                    overriddenBy=row.medtechId,
                    overriddenAt=row.overriddenAt,
                )
                for row in active.values()
            ],
            smartDiagnosisPreview=field.smartDiagnosis,
            smartDiagnosisUnavailable=field.smartDiagnosisUnavailable,
            modelVersion=field.modelVersion,
        )

        if recordOpen:
            # `updatedAt` is set to itself so the column's onupdate does not fire.
            await self.db.execute(
                update(ResultField)
                .where(ResultField.fieldId == fieldId)
                .values(
                    lastOpenedBy=viewerId,
                    lastOpenedAt=datetime.now(UTC),
                    updatedAt=ResultField.updatedAt,
                )
            )
            await self.db.commit()
        return detail

    async def getVerificationSummary(
        self, resultId: uuid.UUID, claims: dict
    ) -> VerificationSummaryResponse:
        """Owner: web. The numbers confirmation will store, before the MedTech confirms.

        Aggregates the ACCEPTED fields with the same call confirm uses, so the two
        agree. A MedTech sees only an assigned specimen, a Supervisor any result.
        `canConfirm` and `blockingReason` mirror the confirm guards, in this order:
        `RESULT_NOT_CONFIRMABLE` (status not allowed, or the specimen was rejected),
        `LEGACY_NO_FIELD`, `LEGACY_NO_FINDINGS`, `FIELDS_BELOW_MINIMUM`,
        `FIELDS_NOT_VERIFIED`. The diagnosis preview runs the rule engine on the exact
        mean counts and saves nothing; any engine failure sets it unavailable.

        Raises:
            NotFoundException: `RESULT_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`, for a MedTech on another's specimen.
        """
        result = await self.db.get(AnalysisResult, resultId)
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message=f"No analysis result found with id {resultId}."
            )
        specimen = await readFieldAccess(
            self.db, result.specimenId, uuid.UUID(claims["user_id"]), claims["role"]
        )
        accepted = await loadAcceptedFields(self.db, resultId)
        legacy = bool(result.isLegacySingleField) and len(accepted) <= 1
        normalMax = loadNormalMax()

        corrections: list[SummaryCorrection] = []
        if legacy:
            resultLevel = list(
                (
                    await self.db.execute(
                        select(ManualOverride)
                        .where(ManualOverride.resultId == resultId, ManualOverride.fieldId.is_(None))
                        .order_by(ManualOverride.overriddenAt)
                    )
                ).scalars().all()
            )
            counts = legacyCounts(accepted[0].aiFindings if accepted else {}, resultLevel)
            inputs = [
                AcceptedField(fieldNo=f.fieldNo, aiFindings=f.aiFindings or {}, effectiveCounts=counts)
                for f in accepted
            ]
            hasFindings = bool(accepted and accepted[0].aiFindings) or bool(resultLevel)
        else:
            overridesByField = await self._overrideRowsByField([f.fieldId for f in accepted])
            inputs = [
                AcceptedField(
                    fieldNo=f.fieldNo, aiFindings=f.aiFindings or {}, effectiveCounts=f.effectiveCounts or {}
                )
                for f in accepted
            ]
            hasFindings = True
            for f in accepted:
                inForce = activeOverrideRows(overridesByField.get(f.fieldId, []))
                ai, effective = f.aiFindings or {}, f.effectiveCounts or {}
                for particleType in sorted({*ai, *effective}):
                    if float(effective.get(particleType) or 0) != float(ai.get(particleType) or 0):
                        corrections.append(SummaryCorrection(
                            fieldId=f.fieldId, fieldNo=f.fieldNo, particleType=particleType,
                            aiValue=toWholeCount(ai.get(particleType)),
                            effectiveValue=toWholeCount(effective.get(particleType)),
                            kind="OVERRIDE" if particleType in inForce else "BOXES",
                        ))

        totals = aggregate(inputs, normalMax)
        allowed = LEGACY_CONFIRMABLE_RESULT_STATUSES if legacy else CONFIRMABLE_RESULT_STATUSES
        if result.status not in allowed or specimen.status == "REJECTED":
            blocking = "RESULT_NOT_CONFIRMABLE"
        else:
            blocking = fieldBlockingReason(
                legacy=legacy,
                acceptedCount=len(accepted),
                verifiedCount=sum(f.verifiedBy is not None for f in accepted),
                legacyHasFindings=hasFindings,
                minFields=settings.minFieldsPerSpecimen,
            )

        preview, unavailable = (
            SmartDiagnosisService.previewDiagnosis(exactMeans(totals)) if accepted else (None, False)
        )
        return VerificationSummaryResponse(
            resultId=resultId,
            fieldCount=len(accepted),
            minFields=settings.minFieldsPerSpecimen,
            perClass={
                particleType: ParticleAggregate(
                    mean=agg.mean, min=agg.min, max=agg.max, total=agg.total,
                    fieldsSeen=agg.fieldsSeen, aiMean=agg.aiMean,
                    displayValue=displayValue(agg.mean),
                    fieldsAboveNormal=list(agg.fieldsAboveNormal),
                )
                for particleType, agg in totals.perClass.items()
            },
            corrections=corrections,
            focalFindings=[
                FocalFinding(particleType=p, fieldNos=list(nos)) for p, nos in totals.focalFindings
            ],
            smartDiagnosisPreview=preview,
            smartDiagnosisUnavailable=unavailable,
            canConfirm=blocking is None,
            blockingReason=blocking,  # type: ignore[arg-type]
        )

    async def reopenCapture(
        self, resultId: uuid.UUID, medtechId: uuid.UUID, request: Request
    ) -> ResultStatusResponse:
        """Owner: web. PENDING_CONFIRM or RETURNED_FOR_CORRECTION back to CAPTURING,
        keeping fields, corrections and verified marks.

        A legacy single-image result leaves legacy here: from now on it follows the
        multi-field rules. The assigned MedTech gets a push that names the sample only.

        Raises:
            NotFoundException: `RESULT_NOT_FOUND`.
            ForbiddenException: `SPECIMEN_NOT_ASSIGNED`.
            ConflictException: `SPECIMEN_REJECTED`; `INVALID_RESULT_STATUS`, unless the
                result is `PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`.
        """
        result = await self.db.get(AnalysisResult, resultId)
        if result is None:
            raise NotFoundException(
                code="RESULT_NOT_FOUND", message=f"No analysis result found with id {resultId}."
            )
        specimen = await getAssignedSpecimen(self.db, result.specimenId, medtechId)
        requireSpecimenNotRejected(specimen)
        # The first read only found the specimen to lock; read the status again.
        result = (
            await self.db.execute(
                select(AnalysisResult)
                .where(AnalysisResult.resultId == resultId)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        requireVerificationPhase(result)

        wasLegacy = bool(result.isLegacySingleField)
        result.status = ResultStatus.CAPTURING
        result.isLegacySingleField = False
        await self.db.flush()
        await self.db.refresh(result, attribute_names=["updatedAt"])

        await self._notifService.notifyMedtechCaptureReopened(
            medtechId=medtechId,
            resultId=resultId,
            sampleUid=specimen.sampleUid or str(specimen.specimenId),
        )
        await self.auditLogger.record(
            eventType="CAPTURE_REOPENED",
            entityType="analysis_result",
            entityId=resultId,
            userId=medtechId,
            detailJson={
                "specimen_id": str(result.specimenId),
                "field_count": result.fieldCount,
                "legacy_cleared": wasLegacy,
            },
            db=self.db,
            request=request,
        )
        response = ResultStatusResponse(
            resultId=resultId, status=ResultStatus.CAPTURING, updatedAt=result.updatedAt
        )
        await self.db.commit()
        return response

    # ── Queries ──────────────────────────────────────────────────────────────

    async def _fieldIdsWithCurrentEdits(self, fieldIds: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not fieldIds:
            return set()
        rows = await self.db.execute(
            select(FieldAnnotationEdit.fieldId)
            .where(
                FieldAnnotationEdit.fieldId.in_(fieldIds),
                FieldAnnotationEdit.supersededAt.is_(None),
            )
            .distinct()
        )
        return set(rows.scalars().all())

    async def _overrideRowsByField(
        self, fieldIds: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[ManualOverride]]:
        if not fieldIds:
            return {}
        rows = (
            await self.db.execute(
                select(ManualOverride)
                .where(ManualOverride.fieldId.in_(fieldIds))
                .order_by(ManualOverride.overriddenAt)
            )
        ).scalars().all()
        grouped: dict[uuid.UUID, list[ManualOverride]] = {}
        for row in rows:
            if row.fieldId is not None:
                grouped.setdefault(row.fieldId, []).append(row)
        return grouped
