"""AI engine file paths: bundled fallbacks, explicit overrides, the environment
hand-off to the engine, and the startup check that reports missing files.
"""
import logging
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import main
from src.core import config

_ENGINE_VARS = ("MODEL_WEIGHTS_PATH", "GATE_WEIGHTS_PATH", "RULE_ENGINE_CONFIG_PATH", "CLASS_THRESHOLDS_PATH")


def _envWithout(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


def _packageWith(packageDir: str, *relativePaths: str) -> MagicMock:
    for relative in relativePaths:
        path = Path(packageDir) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return MagicMock(submodule_search_locations=[packageDir])


# ── Bundled fallbacks ─────────────────────────────────────────────────────────

def test_bundledModelWeightsPreferBestPtOverTheOldName() -> None:
    with tempfile.TemporaryDirectory() as packageDir:
        spec = _packageWith(packageDir, "models/yolov8/best.pt", "models/yolov8/weights.pt")
        with patch.object(config.importlib.util, "find_spec", return_value=spec):
            assert config._bundledModelWeightsPath() == str(Path(packageDir) / "models/yolov8/best.pt")


@pytest.mark.parametrize(
    ("finder", "relative"),
    [
        ("_bundledGateWeightsPath", "models/gate/gate.pt"),
        ("_bundledRuleEngineConfigPath", "smart_diagnosis/config.yaml"),
        ("_bundledClassThresholdsPath", "models/yolov8/thresholds.yaml"),
    ],
)
def test_bundledEngineFilesAreFoundInsideTheInstalledPackage(finder: str, relative: str) -> None:
    with tempfile.TemporaryDirectory() as packageDir:
        spec = _packageWith(packageDir, relative)
        with patch.object(config.importlib.util, "find_spec", return_value=spec):
            assert getattr(config, finder)() == str(Path(packageDir) / relative)


@pytest.mark.parametrize("spec", [None, MagicMock(submodule_search_locations=["/nowhere"])])
def test_aMissingBundledFileGivesAnEmptyPath(spec: object) -> None:
    with patch.object(config.importlib.util, "find_spec", return_value=spec):
        assert config._bundledGateWeightsPath() == ""
        assert config._bundledRuleEngineConfigPath() == ""


# ── Resolution and hand-off to the engine ─────────────────────────────────────

def test_anExplicitPathWinsAndIsHandedToTheEngine() -> None:
    with patch.dict(os.environ, {"GATE_WEIGHTS_PATH": "/custom/gate.pt"}):
        assert config._resolveEnginePath("GATE_WEIGHTS_PATH", "/bundled/gate.pt") == "/custom/gate.pt"
        assert os.environ["GATE_WEIGHTS_PATH"] == "/custom/gate.pt"


def test_theBundledPathIsUsedAndHandedToTheEngineWhenNoneIsSet() -> None:
    with patch.dict(os.environ, _envWithout("GATE_WEIGHTS_PATH"), clear=True):
        assert config._resolveEnginePath("GATE_WEIGHTS_PATH", "/bundled/gate.pt") == "/bundled/gate.pt"
        assert os.environ["GATE_WEIGHTS_PATH"] == "/bundled/gate.pt"


def test_noPathLeavesTheEngineVariableUnset() -> None:
    with patch.dict(os.environ, _envWithout("GATE_WEIGHTS_PATH"), clear=True):
        assert config._resolveEnginePath("GATE_WEIGHTS_PATH", "") == ""
        assert "GATE_WEIGHTS_PATH" not in os.environ


def test_settingsCarryEveryEnginePath() -> None:
    bundled = {
        "_bundledModelWeightsPath": "/engine/best.pt",
        "_bundledGateWeightsPath": "/engine/gate.pt",
        "_bundledRuleEngineConfigPath": "/engine/config.yaml",
        "_bundledClassThresholdsPath": "/engine/thresholds.yaml",
    }
    with patch.dict(os.environ, _envWithout(*_ENGINE_VARS), clear=True):
        patches = [patch.object(config, name, return_value=value) for name, value in bundled.items()]
        for active in patches:
            active.start()
        try:
            loaded = config._loadSettings()
            handedOver = {name: os.environ[name] for name in _ENGINE_VARS}
        finally:
            for active in patches:
                active.stop()

    assert (loaded.modelWeightsPath, loaded.gateWeightsPath) == ("/engine/best.pt", "/engine/gate.pt")
    assert (loaded.ruleEngineConfigPath, loaded.classThresholdsPath) == ("/engine/config.yaml", "/engine/thresholds.yaml")
    assert handedOver == {
        "MODEL_WEIGHTS_PATH": "/engine/best.pt",
        "GATE_WEIGHTS_PATH": "/engine/gate.pt",
        "RULE_ENGINE_CONFIG_PATH": "/engine/config.yaml",
        "CLASS_THRESHOLDS_PATH": "/engine/thresholds.yaml",
    }


# ── Startup check ─────────────────────────────────────────────────────────────

def _settingsWith(**paths: str) -> MagicMock:
    current = MagicMock()
    current.modelWeightsPath = paths.get("model", "")
    current.gateWeightsPath = paths.get("gate", "")
    current.ruleEngineConfigPath = paths.get("rules", "")
    current.classThresholdsPath = paths.get("thresholds", "")
    return current


def test_noProblemsWhenEveryRequiredFileExists() -> None:
    with tempfile.TemporaryDirectory() as folder:
        files = {name: str(Path(folder) / name) for name in ("model", "gate", "rules")}
        for path in files.values():
            Path(path).write_bytes(b"x")
        # The optional thresholds file is never reported, even when missing.
        assert config.engineFileProblems(_settingsWith(**files, thresholds="/missing.yaml")) == []


def test_anUnsetRequiredFileIsReportedByItsVariableName() -> None:
    problems = config.engineFileProblems(_settingsWith())
    assert len(problems) == 3
    assert any(p.startswith("GATE_WEIGHTS_PATH is unset") for p in problems)


def test_aPathToAMissingFileIsReported() -> None:
    with tempfile.TemporaryDirectory() as folder:
        model = Path(folder) / "best.pt"
        rules = Path(folder) / "config.yaml"
        model.write_bytes(b"x")
        rules.write_bytes(b"x")
        problems = config.engineFileProblems(
            _settingsWith(model=str(model), gate="/nowhere/gate.pt", rules=str(rules))
        )
    assert problems == ["GATE_WEIGHTS_PATH points to a file that does not exist: /nowhere/gate.pt"]


def test_startupLogsEachMissingEngineFile(caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(main, "engineFileProblems", return_value=["GATE_WEIGHTS_PATH is unset."]), \
         caplog.at_level(logging.ERROR, logger="main"), \
         TestClient(main.app):
        pass

    assert [r.getMessage() for r in caplog.records if r.name == "main"] == [
        "AI engine: GATE_WEIGHTS_PATH is unset."
    ]
