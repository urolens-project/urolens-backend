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
