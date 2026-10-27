"""Pure calculations over a result's fields: effective counts and field flags.

No database, no settings, no I/O: callers pass everything in. Counts are exact
floats; rounding happens only where a value is displayed. Confirm-time
aggregation (the stored summary and focal findings) is added in a later phase.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
