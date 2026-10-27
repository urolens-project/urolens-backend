"""Unit tests — `core.rule_thresholds` (normal-range limits from the rules config)."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from src.core import rule_thresholds
from src.core.config import settings
from src.core.rule_thresholds import (
    CONFIG_PATH_ENV,
    loadNormalMax,
    readNormalMax,
    unknownParticleNames,
)

FIXTURE = Path(__file__).parent / "fixtures" / "smart_diagnosis_config.yaml"


@pytest.fixture(autouse=True)
def freshCache(monkeypatch):
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    rule_thresholds.clearCache()
    yield
    rule_thresholds.clearCache()


@pytest.fixture
def configDir():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def _write(configDir: Path, body: str) -> str:
    path = configDir / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_readsTheLimitsFromTheSyntheticConfig():
    assert readNormalMax(str(FIXTURE)) == {"crystals": 5.0, "erythrocytes": 5.0}


def test_theLowestLimitAcrossConditionsIsUsed(configDir):
    path = _write(configDir, """
conditions:
  gout:
    particles:
      crystals: {normal_range_max: 5, weight: 1}
  nephrolithiasis:
    particles:
      crystals: {normal_range_max: 3, weight: 1}
      erythrocytes: {normal_range_max: 8, weight: 1}
""")
    assert readNormalMax(path) == {"crystals": 3.0, "erythrocytes": 8.0}


def test_entriesWithoutANumericLimitAreSkipped(configDir):
    path = _write(configDir, """
conditions:
  gout:
    particles:
      crystals: {normal_range_max: "five"}
      yeast: {normal_range_max: true}
      bacteria: {weight: 1}
      urinary_casts: {normal_range_max: 2}
""")
    assert readNormalMax(path) == {"urinary_casts": 2.0}


def test_loadNormalMaxReadsThePathFromTheEnvironment(monkeypatch):
    monkeypatch.setenv(CONFIG_PATH_ENV, str(FIXTURE))
    assert loadNormalMax() == {"crystals": 5.0, "erythrocytes": 5.0}


def test_unsetGivesAnEmptyDictAndOneLogLine(caplog):
    with caplog.at_level(logging.INFO, logger="src.core.rule_thresholds"):
        assert loadNormalMax() == {}
        assert loadNormalMax() == {}
    assert len([r for r in caplog.records if CONFIG_PATH_ENV in r.getMessage()]) == 1


def test_anUnreadablePathGivesAnEmptyDictAndOneWarningNeverAnError(monkeypatch, configDir, caplog):
    monkeypatch.setenv(CONFIG_PATH_ENV, str(configDir / "missing.yaml"))
    with caplog.at_level(logging.INFO, logger="src.core.rule_thresholds"):
        assert loadNormalMax() == {}
        assert loadNormalMax() == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.parametrize("body", ["conditions: [", "just a string", "conditions: 5", ""])
def test_malformedYamlGivesAnEmptyDict(monkeypatch, configDir, body):
    monkeypatch.setenv(CONFIG_PATH_ENV, _write(configDir, body))
    assert loadNormalMax() == {}


def test_theResultIsCachedPerPath(monkeypatch, configDir):
    path = _write(configDir, "conditions:\n  gout:\n    particles:\n      crystals: {normal_range_max: 5}\n")
    monkeypatch.setenv(CONFIG_PATH_ENV, path)
    assert loadNormalMax() == {"crystals": 5.0}
    Path(path).write_text("conditions:\n  gout:\n    particles:\n      crystals: {normal_range_max: 9}\n")
    assert loadNormalMax() == {"crystals": 5.0}
    rule_thresholds.clearCache()
    assert loadNormalMax() == {"crystals": 9.0}


# ── Q2: configured particle names must be known ──────────────────────────────

def test_anUnknownConfiguredParticleNameIsDetected(configDir):
    path = _write(configDir, """
conditions:
  gout:
    particles:
      uric_acid_crystals: {normal_range_max: 5}
      crystals: {normal_range_max: 5}
""")
    assert unknownParticleNames(readNormalMax(path)) == ["uric_acid_crystals"]


def test_anUnknownNameIsDroppedFromTheFlagLimitsAndLogged(monkeypatch, configDir, caplog):
    monkeypatch.setenv(CONFIG_PATH_ENV, _write(configDir, """
conditions:
  gout:
    particles:
      uric_acid_crystals: {normal_range_max: 5}
      crystals: {normal_range_max: 5}
"""))
    with caplog.at_level(logging.ERROR, logger="src.core.rule_thresholds"):
        assert loadNormalMax() == {"crystals": 5.0}
    assert any("uric_acid_crystals" in r.getMessage() for r in caplog.records)


def test_theSyntheticConfigUsesKnownNames():
    assert unknownParticleNames(readNormalMax(str(FIXTURE))) == []


def test_everyParticleNameInTheRealRulesConfigIsAKnownLabel():
    """Fails if the engine's config names a particle the backend does not know.

    Reads the config the backend resolved (`settings.ruleEngineConfigPath`: the
    file shipped inside the installed engine since v0.2.0, unless overridden);
    skips only when there is none, e.g. without the engine installed.
    """
    path = settings.ruleEngineConfigPath
    if not path or not Path(path).is_file():
        pytest.skip(f"{CONFIG_PATH_ENV} is not set to a readable rules config")
    unknown = unknownParticleNames(readNormalMax(path))
    assert unknown == [], (
        f"Rules config {path} names particles that are not in PARTICLE_LABELS: {unknown}. "
        "Field flags would silently ignore them; align the names with the AI lead."
    )
