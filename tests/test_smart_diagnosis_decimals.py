"""Unit tests — Smart Diagnosis on decimal (mean) counts.

Multi-field confirmation runs the diagnosis on exact mean counts, which are
decimals. `_normalizeCounts` takes them only when `allowDecimal` is set; the
default stays integers. The pinned engine itself rejects decimals until the AI
lead ships support (P-1); the unit tests patch the engine and assert the exact
dict it receives, and one real-engine test switches itself on when P-1 lands.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.smart_diagnosis_service import SmartDiagnosisService, _normalizeCounts

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000010a1")
FIXTURE = Path(__file__).parent / "fixtures" / "smart_diagnosis_config.yaml"


def _engineOutput():
    condition = SimpleNamespace(level=SimpleNamespace(value="LOW"), weighted_score=0.0, evidence=[])
    return SimpleNamespace(
        gout=condition, glomerulonephritis=condition, nephrolithiasis=condition,
        no_significant_indicators=True, engine_version="test",
    )


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _runnable(persist=None):
    service = SmartDiagnosisService(auditLogger=MagicMock(record=AsyncMock()), _notifService=MagicMock())
    service._loadResult = AsyncMock(return_value=SimpleNamespace(
        aiFindings={"erythrocytes": 12}, particleClasses={"erythrocytes": 5.0}, resultId=RESULT_ID))
    service._persistOutput = persist or AsyncMock(return_value="record")
    service._handleFailure = AsyncMock()
    db = MagicMock()
    db.begin_nested = lambda: _Nested()
    return service, db


# ── _normalizeCounts ─────────────────────────────────────────────────────────

def test_theDefaultStillRefusesAFraction():
    with pytest.raises(Exception) as excInfo:
        _normalizeCounts({"erythrocytes": 3.8})
    assert getattr(excInfo.value, "code", None) == "INVALID_CLASSIFICATION"


def test_theDefaultStillTurnsAnIntegralFloatIntoAnInt():
    result = _normalizeCounts({"erythrocytes": 4.0})
    assert result == {"erythrocytes": 4} and isinstance(result["erythrocytes"], int)


def test_decimalsPassThroughExactlyWhenAllowed():
    assert _normalizeCounts({"erythrocytes": 3.8, "urinary_casts": 0.3}, allowDecimal=True) == {
        "erythrocytes": 3.8, "urinary_casts": 0.3}


def test_aWholeNumberMeanGoesToTheEngineAsAnInt():
    result = _normalizeCounts({"erythrocytes": 4.0, "crystals": 0.0, "yeast": 2}, allowDecimal=True)
    assert result == {"erythrocytes": 4, "crystals": 0, "yeast": 2}
    assert all(isinstance(v, int) for v in result.values())


@pytest.mark.parametrize("bad", [-0.1, -1, float("nan"), float("inf"), float("-inf"), True, "3", None])
def test_decimalModeStillRejectsNegativeNonFiniteAndNonNumbers(bad):
    with pytest.raises(Exception) as excInfo:
        _normalizeCounts({"erythrocytes": bad}, allowDecimal=True)
    assert getattr(excInfo.value, "code", None) == "INVALID_CLASSIFICATION"


# ── run(counts=...) ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runGivesTheEngineTheExactFloatDictItWasGiven():
    service, db = _runnable()
    engine = MagicMock(return_value=_engineOutput())

    with patch("urolens_ai.generate_smart_diagnosis", engine):
        record = await service.run(
            RESULT_ID, db, counts={"erythrocytes": 3.8, "urinary_casts": 0.3}, allowDecimal=True)

    assert record == "record"
    engine.assert_called_once_with({"erythrocytes": 3.8, "urinary_casts": 0.3})
    service._handleFailure.assert_not_awaited()


@pytest.mark.asyncio
async def test_runWithoutCountsStillReadsTheStoredFindings():
    service, db = _runnable()
    engine = MagicMock(return_value=_engineOutput())

    with patch("urolens_ai.generate_smart_diagnosis", engine):
        await service.run(RESULT_ID, db)

    engine.assert_called_once_with({"erythrocytes": 5})        # ai_findings overlaid with particle_classes


@pytest.mark.asyncio
async def test_aFractionalCountWithoutAllowDecimalTakesTheFailurePath():
    service, db = _runnable()
    engine = MagicMock(return_value=_engineOutput())

    with patch("urolens_ai.generate_smart_diagnosis", engine):
        assert await service.run(RESULT_ID, db, counts={"erythrocytes": 3.8}) is None

    engine.assert_not_called()
    service._handleFailure.assert_awaited_once()


@pytest.mark.asyncio
async def test_anEngineFailureNeverRaisesAndIsHandled():
    service, db = _runnable()
    engine = MagicMock(side_effect=RuntimeError("INVALID_CLASSIFICATION"))

    with patch("urolens_ai.generate_smart_diagnosis", engine):
        assert await service.run(RESULT_ID, db, counts={"erythrocytes": 3.8}, allowDecimal=True) is None

    service._handleFailure.assert_awaited_once()
    assert service._handleFailure.await_args.args[0] == RESULT_ID


# ── previewDiagnosis ─────────────────────────────────────────────────────────

def test_thePreviewHasTheStoredDiagnosisShapeAndSavesNothing():
    engine = MagicMock(return_value=_engineOutput())
    with patch("urolens_ai.generate_smart_diagnosis", engine):
        preview, unavailable = SmartDiagnosisService.previewDiagnosis({"erythrocytes": 3.8})

    assert unavailable is False
    assert set(preview) == {"gout", "glomerulonephritis", "nephrolithiasis", "no_significant_indicators"}
    assert preview["no_significant_indicators"] is True
    engine.assert_called_once_with({"erythrocytes": 3.8})


def test_aPreviewFailureIsUnavailableNotAnError():
    with patch("urolens_ai.generate_smart_diagnosis", MagicMock(side_effect=RuntimeError("boom"))):
        assert SmartDiagnosisService.previewDiagnosis({"erythrocytes": 3.8}) == (None, True)


def test_noCountsMeansNothingToPreview():
    assert SmartDiagnosisService.previewDiagnosis({}) == (None, False)


def test_aNegativeCountIsUnavailable():
    assert SmartDiagnosisService.previewDiagnosis({"erythrocytes": -1.0}) == (None, True)


# ── the real engine ──────────────────────────────────────────────────────────

def test_theRealEngineAcceptsDecimalCounts(monkeypatch):
    """The pinned engine (v0.2.0 and later) takes decimal counts (P-1).

    Fails if the pin ever goes back to an engine that refuses them. The synthetic
    test config is pointed at so the outcome does not depend on the shipped rules.
    """
    pytest.importorskip("urolens_ai")
    from urolens_ai import generate_smart_diagnosis
    from urolens_ai.smart_diagnosis import rule_engine

    monkeypatch.setattr(rule_engine, "_CONFIG_PATH", str(FIXTURE))
    output = generate_smart_diagnosis({"crystals": 1.5})
    assert output is not None

    preview, unavailable = SmartDiagnosisService.previewDiagnosis({"crystals": 1.5})
    assert unavailable is False and preview is not None
