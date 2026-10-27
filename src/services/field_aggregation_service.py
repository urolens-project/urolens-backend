"""Pure calculations over a result's fields: effective counts, field flags and the
aggregate over the accepted fields.

No database, no settings, no I/O: callers pass everything in. Counts are exact
floats; rounding happens only where a value is displayed.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..core.particle_counts import toWholeCount

LOW_CONFIDENCE = "LOW_CONFIDENCE"
OUTLIER_COUNT = "OUTLIER_COUNT"
ABOVE_NORMAL_MAX = "ABOVE_NORMAL_MAX"


@dataclass(frozen=True)
class FieldCounts:
    """The numbers about one field that the flags need."""

    aiFindings: Mapping[str, Any]
    effectiveCounts: Mapping[str, Any]
    confidenceScores: Mapping[str, Any]


@dataclass(frozen=True)
class FlagThresholds:
    """Review-prompt thresholds (settings); `normalMax` comes from the rules config."""

    lowConfidence: float
    outlierMultiplier: float
    outlierMinAbove: float
    normalMax: Mapping[str, float]


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


# ── Effective counts (rule R3) ───────────────────────────────────────────────

def activeOverrides(rows: Iterable[tuple[str, str, Any, Any]]) -> dict[str, int]:
    """Class to overridden count, from `(particleType, kind, correctedValue, overriddenAt)` rows.

    For one class the latest row decides: an `OVERRIDE` sets the count, a
    `REVERT` removes it. `correctedValue` may be text such as `"7.0"`.
    """
    active: dict[str, int] = {}
    for particleType, kind, correctedValue, _at in sorted(rows, key=lambda row: row[3]):
        if kind == "REVERT":
            active.pop(particleType, None)
        else:
            active[particleType] = toWholeCount(correctedValue)
    return active


def computeEffectiveCounts(
    aiFindings: Mapping[str, Any],
    detections: Sequence[Mapping[str, Any]],
    removedIds: Iterable[str],
    addedParticleTypes: Iterable[str],
    overrides: Mapping[str, int],
) -> dict[str, float]:
    """Effective count per class for one field.

    ```
    removedAi[c] = ids in removedIds whose detection has class c
    added[c]     = added boxes of class c
    boxCount[c]  = max(0, ai[c] - removedAi[c] + added[c])
    effective[c] = overrides[c] if c has an active override else boxCount[c]
    ```

    The AI **count** is used, not the number of stored detections. An id that
    matches no detection is ignored here (the save route rejects it first). Box
    edits to a class with an active override are kept but not used until the
    override is reverted.
    """
    removedSet = set(removedIds)
    classById = {str(d.get("id")): str(d.get("particleType")) for d in detections}
    removedAi: dict[str, int] = {}
    for detectionId in removedSet:
        particleType = classById.get(detectionId)
        if particleType is not None:
            removedAi[particleType] = removedAi.get(particleType, 0) + 1
    added: dict[str, int] = {}
    for particleType in addedParticleTypes:
        added[particleType] = added.get(particleType, 0) + 1

    effective: dict[str, float] = {}
    for particleType in {*aiFindings, *added, *overrides}:
        if particleType in overrides:
            effective[particleType] = overrides[particleType]
        else:
            boxCount = _number(aiFindings.get(particleType)) - removedAi.get(particleType, 0)
            effective[particleType] = max(0.0, boxCount + added.get(particleType, 0))
    return effective


# ── Means and flags (rule R10) ───────────────────────────────────────────────

def classMeans(effectiveCountsPerField: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Exact mean effective count per class over the given (accepted) fields."""
    fieldCount = len(effectiveCountsPerField)
    if fieldCount == 0:
        return {}
    totals: dict[str, float] = {}
    for counts in effectiveCountsPerField:
        for particleType, value in counts.items():
            totals[particleType] = totals.get(particleType, 0.0) + _number(value)
    return {particleType: total / fieldCount for particleType, total in totals.items()}


def fieldMeanConfidence(
    aiFindings: Mapping[str, Any], confidenceScores: Mapping[str, Any]
) -> float | None:
    """Detection-weighted mean confidence of a field.

    `sum(conf[c] * ai[c]) / sum(ai[c])` over classes with a positive AI count and a
    confidence score; since each score is the mean over that class's detections
    this equals the mean over every detection. `None` when there is nothing to weigh.
    """
    weighted = 0.0
    weight = 0.0
    for particleType, count in aiFindings.items():
        n = _number(count)
        score = confidenceScores.get(particleType)
        if n > 0 and isinstance(score, (int, float)) and not isinstance(score, bool):
            weighted += float(score) * n
            weight += n
    return weighted / weight if weight > 0 else None


def flagsFor(
    field: FieldCounts, means: Mapping[str, float], thresholds: FlagThresholds
) -> list[tuple[str, str | None]]:
    """Review flags for one field as `(reason, particleType)`; `particleType` is None for LOW_CONFIDENCE.

    - LOW_CONFIDENCE: detection-weighted mean confidence below `lowConfidence`.
    - OUTLIER_COUNT, per class: effective count more than `outlierMultiplier` times
      the class mean AND at least `outlierMinAbove` above it.
    - ABOVE_NORMAL_MAX, per class: effective count above the rules-config limit
      (classes without a limit are never flagged).
    """
    flags: list[tuple[str, str | None]] = []

    confidence = fieldMeanConfidence(field.aiFindings, field.confidenceScores)
    if confidence is not None and confidence < thresholds.lowConfidence:
        flags.append((LOW_CONFIDENCE, None))

    effective = {particleType: _number(v) for particleType, v in field.effectiveCounts.items()}
    for particleType in sorted(effective):
        count = effective[particleType]
        mean = means.get(particleType, 0.0)
        if count > thresholds.outlierMultiplier * mean and count - mean >= thresholds.outlierMinAbove:
            flags.append((OUTLIER_COUNT, particleType))
    for particleType in sorted(effective):
        limit = thresholds.normalMax.get(particleType)
        if limit is not None and effective[particleType] > limit:
            flags.append((ABOVE_NORMAL_MAX, particleType))
    return flags


# ── Aggregation over the accepted fields (rule R10) ──────────────────────────
# Used by confirm (what is stored) and by the verification summary (what is shown),
# so both give the same numbers. Exact floats throughout: nothing here rounds
# except `displayValue` and `settledParticleClasses`.


@dataclass(frozen=True)
class AcceptedField:
    """One accepted field, reduced to what the aggregation needs."""

    fieldNo: int
    aiFindings: Mapping[str, Any]
    effectiveCounts: Mapping[str, Any]


@dataclass(frozen=True)
class ClassAggregate:
    """One class over the accepted fields (exact, unrounded)."""

    mean: float
    min: float
    max: float
    total: float
    fieldsSeen: int
    aiMean: float
    fieldsAboveNormal: tuple[int, ...] = ()


@dataclass(frozen=True)
class Aggregate:
    """Per-class statistics over the accepted fields, plus focal findings."""

    fieldCount: int
    perClass: Mapping[str, ClassAggregate]
    focalFindings: tuple[tuple[str, tuple[int, ...]], ...] = ()


def roundHalfUp(value: float) -> int:
    """`value` rounded to a whole number, halves up (Python's `round` rounds halves to even)."""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def displayValue(mean: float) -> str:
    """How a class mean is shown: `"<1"` for 0 < mean < 0.5, else rounded half up; 0 stays `"0"`."""
    if mean <= 0:
        return "0"
    if mean < 0.5:
        return "<1"
    return str(roundHalfUp(mean))


def aggregate(fields: Sequence[AcceptedField], normalMax: Mapping[str, float]) -> Aggregate:
    """Mean, min, max, total, fields seen and AI mean per class over `fields`.

    `fields` must be the ACCEPTED fields only. A class missing from a field counts
    as 0 there. A focal finding is a class where some field's effective count is
    above `normalMax[class]` while the class mean is not; `normalMax` comes from the
    rules config, and with no limit for a class (or an empty config) there is none.
    """
    count = len(fields)
    if count == 0:
        return Aggregate(fieldCount=0, perClass={})

    particleTypes = sorted({p for f in fields for p in (*f.effectiveCounts, *f.aiFindings)})
    perClass: dict[str, ClassAggregate] = {}
    focal: list[tuple[str, tuple[int, ...]]] = []
    for particleType in particleTypes:
        effective = [_number(f.effectiveCounts.get(particleType)) for f in fields]
        ai = [_number(f.aiFindings.get(particleType)) for f in fields]
        total = sum(effective)
        mean = total / count
        limit = normalMax.get(particleType)
        above = tuple(
            f.fieldNo for f, value in zip(fields, effective, strict=True)
            if limit is not None and value > limit
        )
        perClass[particleType] = ClassAggregate(
            mean=mean,
            min=min(effective),
            max=max(effective),
            total=total,
            fieldsSeen=sum(value > 0 for value in effective),
            aiMean=sum(ai) / count,
            fieldsAboveNormal=above,
        )
        if above and limit is not None and mean <= limit:
            focal.append((particleType, above))
    return Aggregate(fieldCount=count, perClass=perClass, focalFindings=tuple(focal))


def exactMeans(result: Aggregate) -> dict[str, float]:
    """Exact mean effective count per class: the input to the final Smart Diagnosis."""
    return {particleType: agg.mean for particleType, agg in result.perClass.items()}


def exactAiMeans(result: Aggregate) -> dict[str, float]:
    """Exact mean AI count per class, stored in `analysis_results.ai_findings`."""
    return {particleType: agg.aiMean for particleType, agg in result.perClass.items()}


def settledParticleClasses(result: Aggregate) -> dict[str, int]:
    """Whole-number counts for `particle_classes`.

    0 only when the class mean is 0; otherwise at least 1, so a particle that was
    seen never reads as 0 (a mean in (0, 1) is stored as 1).
    """
    return {
        particleType: 0 if agg.mean == 0 else max(1, roundHalfUp(agg.mean))
        for particleType, agg in result.perClass.items()
    }


def storedFieldSummary(result: Aggregate) -> dict[str, Any]:
    """The JSON stored in `analysis_results.field_summary`: exact floats, no display rounding."""
    return {
        "fieldCount": result.fieldCount,
        "perClass": {
            particleType: {
                "mean": agg.mean, "min": agg.min, "max": agg.max, "total": agg.total,
                "fieldsSeen": agg.fieldsSeen, "aiMean": agg.aiMean,
            }
            for particleType, agg in result.perClass.items()
        },
        "focalFindings": [
            {"particleType": particleType, "fieldNos": list(fieldNos)}
            for particleType, fieldNos in result.focalFindings
        ],
    }


# ── Can this result be confirmed? (shared by confirm and the summary) ────────

def fieldBlockingReason(
    *,
    legacy: bool,
    acceptedCount: int,
    verifiedCount: int,
    legacyHasFindings: bool,
    minFields: int,
) -> str | None:
    """Why the fields stop a confirmation, or `None` if they do not.

    In order: `LEGACY_NO_FIELD`, `LEGACY_NO_FINDINGS`, `FIELDS_BELOW_MINIMUM`,
    `FIELDS_NOT_VERIFIED`. A legacy result skips the minimum and verify rules but
    needs its one field, and that field needs findings (or a result-level override).
    Status and rejected-specimen rules are checked by the caller.
    """
    if legacy:
        if acceptedCount == 0:
            return "LEGACY_NO_FIELD"
        if not legacyHasFindings:
            return "LEGACY_NO_FINDINGS"
        return None
    if acceptedCount < minFields:
        return "FIELDS_BELOW_MINIMUM"
    if verifiedCount < acceptedCount:
        return "FIELDS_NOT_VERIFIED"
    return None
