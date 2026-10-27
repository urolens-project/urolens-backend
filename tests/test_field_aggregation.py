"""Unit tests — pure field calculations (`field_aggregation_service`)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.services.field_aggregation_service import (
    ABOVE_NORMAL_MAX,
    LOW_CONFIDENCE,
    OUTLIER_COUNT,
    FieldCounts,
    FlagThresholds,
    activeOverrides,
    classMeans,
    computeEffectiveCounts,
    fieldMeanConfidence,
    flagsFor,
)

T0 = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ── effective counts (R3) ────────────────────────────────────────────────────

DETECTIONS = [
    {"id": "d1", "particleType": "erythrocytes"},
    {"id": "d2", "particleType": "erythrocytes"},
    {"id": "d3", "particleType": "erythrocytes"},
    {"id": "d4", "particleType": "crystals"},
]


def test_withNoEditsTheEffectiveCountsAreTheAiCounts():
    effective = computeEffectiveCounts({"erythrocytes": 9, "crystals": 1}, DETECTIONS, [], [], {})
    assert effective == {"erythrocytes": 9, "crystals": 1}


def test_theProposalExampleFieldFourRemovesThreeBoxes():
    # Proposal 4.3: field 4 had 9 erythrocytes; three boxes were crystals misread as RBC.
    effective = computeEffectiveCounts(
        {"erythrocytes": 9}, DETECTIONS, ["d1", "d2", "d3"], [], {}
    )
    assert effective["erythrocytes"] == 6


def test_addedBoxesCountAndRemovedBoxesSubtract():
    effective = computeEffectiveCounts(
        {"erythrocytes": 3}, DETECTIONS, ["d1"], ["erythrocytes", "erythrocytes", "yeast"], {}
    )
    assert effective["erythrocytes"] == 3 - 1 + 2
    assert effective["yeast"] == 1


def test_theAiCountIsUsedNotTheNumberOfStoredDetections():
    # 12 counted by the engine but only 4 detections stored (a cap, say).
    effective = computeEffectiveCounts({"erythrocytes": 12}, DETECTIONS, ["d1"], [], {})
    assert effective["erythrocytes"] == 11


def test_anIdThatMatchesNoDetectionIsIgnored():
    assert computeEffectiveCounts({"erythrocytes": 3}, DETECTIONS, ["nope"], [], {}) == {
        "erythrocytes": 3
    }


def test_theCountNeverGoesBelowZero():
    effective = computeEffectiveCounts({"crystals": 0}, DETECTIONS, ["d4"], [], {})
    assert effective["crystals"] == 0


def test_anActiveOverrideWinsOverTheBoxCount():
    effective = computeEffectiveCounts(
        {"erythrocytes": 9}, DETECTIONS, ["d1"], ["erythrocytes"], {"erythrocytes": 4}
    )
    assert effective["erythrocytes"] == 4


def test_anOverrideOnAClassTheAiDidNotFindAppears():
    assert computeEffectiveCounts({}, [], [], [], {"yeast": 3}) == {"yeast": 3}


def test_theAiFindingsAreNotModified():
    ai = {"erythrocytes": 9}
    computeEffectiveCounts(ai, DETECTIONS, ["d1"], ["crystals"], {})
    assert ai == {"erythrocytes": 9}


# ── overrides in force ───────────────────────────────────────────────────────

def test_theLatestOverrideForAClassDecides():
    rows = [
        ("erythrocytes", "OVERRIDE", "4.0", _at(1)),
        ("erythrocytes", "OVERRIDE", "7.0", _at(2)),
    ]
    assert activeOverrides(rows) == {"erythrocytes": 7}


def test_aRevertEndsTheOverrideAndALaterOverrideStartsAgain():
    base = [("crystals", "OVERRIDE", "9", _at(1)), ("crystals", "REVERT", "2", _at(2))]
    assert activeOverrides(base) == {}
    assert activeOverrides([*base, ("crystals", "OVERRIDE", "5.0", _at(3))]) == {"crystals": 5}


def test_rowsAreOrderedByTimeNotByPosition():
    rows = [("crystals", "REVERT", "2", _at(2)), ("crystals", "OVERRIDE", "9", _at(1))]
    assert activeOverrides(rows) == {}


def test_classesAreIndependent():
    rows = [("crystals", "OVERRIDE", 3, _at(1)), ("yeast", "REVERT", 0, _at(2))]
    assert activeOverrides(rows) == {"crystals": 3}


# ── means ────────────────────────────────────────────────────────────────────

def test_meansAreExactNotRounded():
    # The proposal example after corrections: total 38 over 10 fields.
    perField = [{"erythrocytes": n} for n in (3, 5, 2, 6, 4, 4, 1, 6, 3, 4)]
    assert classMeans(perField)["erythrocytes"] == 3.8


def test_aClassMissingFromAFieldCountsAsZero():
    assert classMeans([{"crystals": 3}, {}, {}]) == {"crystals": 1.0}


def test_noFieldsGiveNoMeans():
    assert classMeans([]) == {}


# ── confidence ───────────────────────────────────────────────────────────────

def test_confidenceIsWeightedByDetectionCount():
    # 9 detections at 0.9 and 1 at 0.1 -> the detection-level mean is 0.82.
    value = fieldMeanConfidence({"erythrocytes": 9, "crystals": 1}, {"erythrocytes": 0.9, "crystals": 0.1})
    assert value == pytest.approx(0.82)


def test_aClassWithNoDetectionsDoesNotCount():
    assert fieldMeanConfidence({"erythrocytes": 2, "crystals": 0}, {"erythrocytes": 0.6, "crystals": 0.1}) == 0.6


@pytest.mark.parametrize(
    ("ai", "conf"),
    [({}, {}), ({"crystals": 0}, {"crystals": 0.5}), ({"crystals": 3}, {})],
)
def test_noDetectionsOrNoScoresGiveNoConfidence(ai, conf):
    assert fieldMeanConfidence(ai, conf) is None


# ── flags (R10) ──────────────────────────────────────────────────────────────

def _thresholds(normalMax=None) -> FlagThresholds:
    return FlagThresholds(
        lowConfidence=0.5, outlierMultiplier=2.0, outlierMinAbove=5, normalMax=normalMax or {}
    )


def _field(effective, ai=None, conf=None) -> FieldCounts:
    return FieldCounts(aiFindings=ai if ai is not None else effective,
                       effectiveCounts=effective, confidenceScores=conf or {})


def test_lowConfidenceIsFlaggedBelowTheThresholdOnly():
    low = _field({"crystals": 4}, conf={"crystals": 0.49})
    exactlyAt = _field({"crystals": 4}, conf={"crystals": 0.5})
    assert (LOW_CONFIDENCE, None) in flagsFor(low, {"crystals": 4}, _thresholds())
    assert flagsFor(exactlyAt, {"crystals": 4}, _thresholds()) == []


def test_aFieldWithNoConfidenceDataIsNotFlaggedLow():
    assert flagsFor(_field({"crystals": 4}), {"crystals": 4}, _thresholds()) == []


def test_anOutlierNeedsBothMoreThanTwiceTheMeanAndFiveAboveIt():
    means = {"erythrocytes": 4.0}
    assert (OUTLIER_COUNT, "erythrocytes") in flagsFor(_field({"erythrocytes": 9}), means, _thresholds())
    # 8 is exactly 2x and 4 above: neither condition holds.
    assert flagsFor(_field({"erythrocytes": 8}), means, _thresholds()) == []
    # mean 3.0: 9 is more than 6 (2x) and 6 above the mean, so it is flagged.
    assert (OUTLIER_COUNT, "erythrocytes") in flagsFor(
        _field({"erythrocytes": 9}), {"erythrocytes": 3.0}, _thresholds())


def test_moreThanTwiceTheMeanButLessThanFiveAboveIsNotAnOutlier():
    # mean 1.0: 4 is 4x the mean but only 3 above it.
    assert flagsFor(_field({"yeast": 4}), {"yeast": 1.0}, _thresholds()) == []
    # mean 1.0: 6 is 5 above and more than 2x -> flagged (boundary of the second condition).
    assert (OUTLIER_COUNT, "yeast") in flagsFor(_field({"yeast": 6}), {"yeast": 1.0}, _thresholds())


def test_aboveNormalMaxUsesTheRulesConfigLimit():
    thresholds = _thresholds({"crystals": 5})
    assert (ABOVE_NORMAL_MAX, "crystals") in flagsFor(_field({"crystals": 6}), {"crystals": 6}, thresholds)
    assert flagsFor(_field({"crystals": 5}), {"crystals": 5}, thresholds) == []


def test_aClassWithNoConfiguredLimitIsNeverFlaggedAboveNormal():
    assert flagsFor(_field({"yeast": 50}), {"yeast": 50}, _thresholds({"crystals": 5})) == []


def test_flagsComeInAStableOrder():
    field = _field({"crystals": 20, "erythrocytes": 20}, conf={"crystals": 0.1, "erythrocytes": 0.1})
    flags = flagsFor(field, {"crystals": 2.0, "erythrocytes": 2.0},
                     _thresholds({"crystals": 5, "erythrocytes": 5}))
    assert flags == [
        (LOW_CONFIDENCE, None),
        (OUTLIER_COUNT, "crystals"), (OUTLIER_COUNT, "erythrocytes"),
        (ABOVE_NORMAL_MAX, "crystals"), (ABOVE_NORMAL_MAX, "erythrocytes"),
    ]


# ══ Aggregation over the accepted fields (W3) ════════════════════════════════

from src.services.field_aggregation_service import (  # noqa: E402
    AcceptedField,
    aggregate,
    displayValue,
    exactAiMeans,
    exactMeans,
    fieldBlockingReason,
    roundHalfUp,
    settledParticleClasses,
    storedFieldSummary,
)

AI_RBC = (3, 5, 2, 9, 4, 4, 1, 6, 3, 4)
EFFECTIVE_RBC = (3, 5, 2, 6, 4, 4, 1, 6, 3, 4)


def _accepted(ai, effective, particleType="erythrocytes"):
    return [
        AcceptedField(fieldNo=n, aiFindings={particleType: a}, effectiveCounts={particleType: e})
        for n, (a, e) in enumerate(zip(ai, effective, strict=True), start=1)
    ]


def test_theProposalExampleGivesExactMeansTotalsAndRange():
    result = aggregate(_accepted(AI_RBC, EFFECTIVE_RBC), {})
    rbc = result.perClass["erythrocytes"]
    assert result.fieldCount == 10
    assert (rbc.mean, rbc.min, rbc.max, rbc.total, rbc.fieldsSeen, rbc.aiMean) == (3.8, 1, 6, 38, 10, 4.1)
    assert displayValue(rbc.mean) == "4"
    assert settledParticleClasses(result) == {"erythrocytes": 4}
    assert exactMeans(result) == {"erythrocytes": 3.8}
    assert exactAiMeans(result) == {"erythrocytes": 4.1}


def test_theStoredSummaryKeepsExactFloatsAndNoDisplayRounding():
    stored = storedFieldSummary(aggregate(_accepted(AI_RBC, EFFECTIVE_RBC), {}))
    assert stored["fieldCount"] == 10
    assert stored["perClass"]["erythrocytes"] == {
        "mean": 3.8, "min": 1, "max": 6, "total": 38, "fieldsSeen": 10, "aiMean": 4.1}
    assert "displayValue" not in str(stored)
    assert stored["focalFindings"] == []


def test_aMeanThatIsNotATerminatingDecimalIsNotRounded():
    result = aggregate(_accepted((1, 0, 0), (1, 0, 0), "urinary_casts"), {})
    assert result.perClass["urinary_casts"].mean == 1 / 3
    assert storedFieldSummary(result)["perClass"]["urinary_casts"]["mean"] == 1 / 3


@pytest.mark.parametrize(
    ("mean", "shown", "stored"),
    [
        (0, "0", 0), (0.01, "<1", 1), (0.3, "<1", 1), (0.49, "<1", 1), (0.4999999, "<1", 1),
        (0.5, "1", 1), (0.51, "1", 1), (1.0, "1", 1), (1.49, "1", 1), (1.5, "2", 2),
        (2.4, "2", 2), (2.5, "3", 3), (3.5, "4", 4), (3.8, "4", 4), (9.5, "10", 10),
    ],
)
def test_displayAndStoredValuesAtTheRoundingBoundaries(mean, shown, stored):
    assert displayValue(mean) == shown
    result = aggregate([AcceptedField(1, {}, {"yeast": mean})], {})
    assert settledParticleClasses(result) == {"yeast": stored}


def test_halfUpDiffersFromPythonsRound():
    assert round(2.5) == 2 and roundHalfUp(2.5) == 3
    assert round(0.5) == 0 and roundHalfUp(0.5) == 1


def test_aSeenParticleIsNeverZeroInTheSettledClasses():
    # casts seen once in 3 of 10 fields: mean 0.3
    effective = [1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
    result = aggregate(_accepted(effective, effective, "urinary_casts"), {})
    assert result.perClass["urinary_casts"].mean == 0.3
    assert settledParticleClasses(result)["urinary_casts"] == 1
    assert displayValue(result.perClass["urinary_casts"].mean) == "<1"


def test_aClassCorrectedAwayEntirelyStaysZero():
    result = aggregate(_accepted((2, 1), (0, 0), "crystals"), {})
    assert result.perClass["crystals"].mean == 0 and result.perClass["crystals"].aiMean == 1.5
    assert settledParticleClasses(result) == {"crystals": 0}
    assert displayValue(0.0) == "0"


def test_fieldsSeenCountsOnlyFieldsWithAPositiveCount():
    result = aggregate(_accepted((0, 2, 0, 4), (0, 2, 0, 4), "yeast"), {})
    assert result.perClass["yeast"].fieldsSeen == 2


def test_aClassMissingFromAFieldCountsAsZeroThere():
    fields = [AcceptedField(1, {"yeast": 4}, {"yeast": 4}), AcceptedField(2, {}, {})]
    yeast = aggregate(fields, {}).perClass["yeast"]
    assert (yeast.mean, yeast.min, yeast.max, yeast.total) == (2.0, 0, 4, 4)


def test_noAcceptedFieldsGiveAnEmptyAggregate():
    result = aggregate([], {"crystals": 5})
    assert result.fieldCount == 0 and result.perClass == {} and result.focalFindings == ()


# ── focal findings ───────────────────────────────────────────────────────────

def _nineQuietOneLoud():
    counts = [0] * 9 + [7]
    return _accepted(counts, counts, "crystals")


def test_aFocalFindingSurfacesWhenOneFieldIsHighButTheMeanIsNormal():
    result = aggregate(_nineQuietOneLoud(), {"crystals": 5})
    crystals = result.perClass["crystals"]
    assert crystals.mean == 0.7 and crystals.mean <= 5
    assert crystals.fieldsAboveNormal == (10,)
    assert result.focalFindings == (("crystals", (10,)),)
    assert storedFieldSummary(result)["focalFindings"] == [{"particleType": "crystals", "fieldNos": [10]}]


def test_noFocalFindingWhenTheMeanItselfIsAboveNormal():
    counts = [9] * 10
    result = aggregate(_accepted(counts, counts, "crystals"), {"crystals": 5})
    assert result.perClass["crystals"].fieldsAboveNormal == tuple(range(1, 11))
    assert result.focalFindings == ()


def test_aFieldExactlyAtTheLimitIsNotAbove():
    counts = [0] * 9 + [5]
    assert aggregate(_accepted(counts, counts, "crystals"), {"crystals": 5}).focalFindings == ()


def test_anEmptyRulesConfigGivesNoFocalFindingsAndNoError():
    result = aggregate(_nineQuietOneLoud(), {})
    assert result.focalFindings == ()
    assert result.perClass["crystals"].fieldsAboveNormal == ()


def test_aClassWithoutAConfiguredLimitIsNeverFocal():
    assert aggregate(_nineQuietOneLoud(), {"erythrocytes": 5}).focalFindings == ()


def test_everyFieldAboveTheLimitIsListed():
    counts = [0, 8, 0, 0, 9, 0, 0, 0, 0, 0]
    result = aggregate(_accepted(counts, counts, "crystals"), {"crystals": 5})
    assert result.focalFindings == (("crystals", (2, 5)),)


# ── the guards, in order ─────────────────────────────────────────────────────

def _block(**kw):
    base = dict(legacy=False, acceptedCount=10, verifiedCount=10, legacyHasFindings=True, minFields=10)
    return fieldBlockingReason(**{**base, **kw})


def test_aCompleteVerifiedListIsNotBlocked():
    assert _block() is None
    assert _block(acceptedCount=14, verifiedCount=14) is None


def test_belowTheMinimumIsBlockedBeforeUnverified():
    assert _block(acceptedCount=9, verifiedCount=3) == "FIELDS_BELOW_MINIMUM"


def test_unverifiedFieldsBlockOnceTheMinimumIsMet():
    assert _block(verifiedCount=9) == "FIELDS_NOT_VERIFIED"


def test_aLegacyResultSkipsTheMinimumAndVerifyRules():
    assert _block(legacy=True, acceptedCount=1, verifiedCount=0) is None


def test_aLegacyResultNeedsItsFieldThenItsFindings():
    assert _block(legacy=True, acceptedCount=0, verifiedCount=0, legacyHasFindings=False) == "LEGACY_NO_FIELD"
    assert _block(legacy=True, acceptedCount=1, verifiedCount=0, legacyHasFindings=False) == "LEGACY_NO_FINDINGS"


def test_theMinimumComesFromTheArgument():
    assert _block(acceptedCount=3, verifiedCount=3, minFields=3) is None
    assert _block(acceptedCount=3, verifiedCount=3, minFields=4) == "FIELDS_BELOW_MINIMUM"
