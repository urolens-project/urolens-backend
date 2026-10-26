"""Unit tests — multi-field settings and the env parsers in `core.config`."""
from __future__ import annotations

import pytest

from src.core import config
from src.core.config import _envBool, _envNumber, _loadSettings

_FIELD_ENV = (
    "FIELD_WORKFLOW_ENABLED",
    "MIN_FIELDS_PER_SPECIMEN",
    "FIELD_LOW_CONFIDENCE_THRESHOLD",
    "FIELD_OUTLIER_MULTIPLIER",
    "FIELD_OUTLIER_MIN_ABOVE",
)


@pytest.fixture(autouse=True)
def cleanEnv(monkeypatch):
    for name in _FIELD_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("word", ["true", "TRUE", "True", "1", "yes", "on", " true "])
def test_boolAcceptsTrueSpellings(monkeypatch, word):
    monkeypatch.setenv("X_FLAG", word)
    assert _envBool("X_FLAG", False) is True


@pytest.mark.parametrize("word", ["false", "FALSE", "0", "no", "off", " off "])
def test_boolAcceptsFalseSpellings(monkeypatch, word):
    monkeypatch.setenv("X_FLAG", word)
    assert _envBool("X_FLAG", True) is False


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_boolUnsetOrBlankGivesTheDefault(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("X_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_FLAG", raw)
    assert _envBool("X_FLAG", True) is True
    assert _envBool("X_FLAG", False) is False


def test_boolRejectsAnythingElseNamingTheVariable(monkeypatch):
    monkeypatch.setenv("X_FLAG", "maybe")
    with pytest.raises(RuntimeError, match="X_FLAG"):
        _envBool("X_FLAG", False)


def test_numberParsesAndChecksTheLowerBound(monkeypatch):
    monkeypatch.setenv("X_NUM", "7")
    assert _envNumber("X_NUM", 10, int, 1) == 7
    monkeypatch.setenv("X_NUM", "0")
    with pytest.raises(RuntimeError, match="X_NUM"):
        _envNumber("X_NUM", 10, int, 1)
    monkeypatch.setenv("X_NUM", "five")
    with pytest.raises(RuntimeError, match="X_NUM"):
        _envNumber("X_NUM", 10, int, 1)
    monkeypatch.setenv("X_NUM", "1.0")
    with pytest.raises(RuntimeError, match="X_NUM"):
        _envNumber("X_NUM", 2.0, float, 1.0, lowInclusive=False)


def test_defaultsKeepTheFlagOff():
    loaded = _loadSettings()
    assert loaded.fieldWorkflowEnabled is False
    assert loaded.minFieldsPerSpecimen == 10
    assert loaded.fieldLowConfidenceThreshold == 0.5
    assert loaded.fieldOutlierMultiplier == 2.0
    assert loaded.fieldOutlierMinAbove == 5


def test_theLiveSettingsObjectAlsoDefaultsOff():
    assert config.settings.fieldWorkflowEnabled is False


def test_envOverridesAreRead(monkeypatch):
    monkeypatch.setenv("FIELD_WORKFLOW_ENABLED", "true")
    monkeypatch.setenv("MIN_FIELDS_PER_SPECIMEN", "12")
    monkeypatch.setenv("FIELD_LOW_CONFIDENCE_THRESHOLD", "0.6")
    loaded = _loadSettings()
    assert loaded.fieldWorkflowEnabled is True
    assert loaded.minFieldsPerSpecimen == 12
    assert loaded.fieldLowConfidenceThreshold == 0.6


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MIN_FIELDS_PER_SPECIMEN", "0"),
        ("FIELD_LOW_CONFIDENCE_THRESHOLD", "0"),
        ("FIELD_LOW_CONFIDENCE_THRESHOLD", "1.5"),
        ("FIELD_OUTLIER_MULTIPLIER", "1"),
        ("FIELD_OUTLIER_MIN_ABOVE", "0"),
        ("FIELD_WORKFLOW_ENABLED", "maybe"),
    ],
)
def test_badValuesFailAtStartup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        _loadSettings()
