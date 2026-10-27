"""AI engine file paths: bundled fallbacks, explicit overrides, the environment
hand-off to the engine, and the startup check that reports an old or missing
engine, a variable overriding the engine's own file, missing files and Git LFS
pointers left in place of the real weights. With FIELD_WORKFLOW_ENABLED on, any
problem stops the server from starting.
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


def test_aLeftoverWeightsPtIsNeverTheBundledModel() -> None:
    # A hand-copied weights.pt from before v0.2.0 is not the pinned model.
    with tempfile.TemporaryDirectory() as packageDir:
        spec = _packageWith(packageDir, "models/yolov8/weights.pt")
        with patch.object(config.importlib.util, "find_spec", return_value=spec):
            assert config._bundledModelWeightsPath() == ""


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


@pytest.fixture
def cleanEngine():
    """The pinned engine is installed and has no bundled files to compare against."""
    with patch.object(config, "_installedEngineVersion", return_value="0.2.0"), \
         patch.object(config, "_bundledEngineFile", return_value=""):
        yield


@pytest.mark.usefixtures("cleanEngine")
def test_noProblemsWhenEveryRequiredFileExists() -> None:
    with tempfile.TemporaryDirectory() as folder:
        files = {name: str(Path(folder) / name) for name in ("model", "gate", "rules")}
        for path in files.values():
            Path(path).write_bytes(b"x")
        # The optional thresholds file is never reported, even when missing.
        assert config.engineFileProblems(_settingsWith(**files, thresholds="/missing.yaml")) == []


@pytest.mark.usefixtures("cleanEngine")
def test_anUnsetRequiredFileIsReportedByItsVariableName() -> None:
    problems = config.engineFileProblems(_settingsWith())
    assert len(problems) == 3
    assert any(p.startswith("GATE_WEIGHTS_PATH is unset") for p in problems)


@pytest.mark.usefixtures("cleanEngine")
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


# What `git lfs` leaves in a checkout that never ran `git lfs pull`.
_LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:123e7b00ef92ca50ce7c45b435a151825d6856114007aae8ee69d4b34882f64c\n"
    b"size 3186242\n"
)


@pytest.mark.usefixtures("cleanEngine")
def test_aGitLfsPointerInPlaceOfTheWeightsIsReported() -> None:
    with tempfile.TemporaryDirectory() as folder:
        model, gate, rules = (Path(folder) / name for name in ("best.pt", "gate.pt", "config.yaml"))
        model.write_bytes(b"\x80\x02real-weights")
        gate.write_bytes(_LFS_POINTER)
        rules.write_bytes(b"conditions: {}")
        problems = config.engineFileProblems(
            _settingsWith(model=str(model), gate=str(gate), rules=str(rules))
        )

    assert len(problems) == 1
    assert problems[0].startswith(f"GATE_WEIGHTS_PATH points to a Git LFS pointer, not the real file: {gate}.")
    assert "git lfs pull" in problems[0]


@pytest.mark.parametrize(
    "content",
    [b"", b"version 2 of my weights", b"version https://git-lfs.github.com/spe", b"\x80\x02" + _LFS_POINTER],
)
def test_aRealFileIsNotMistakenForAPointer(content: bytes) -> None:
    # Only a file that starts with the full LFS spec line is a pointer.
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(content)
        handle.flush()
        assert config._isGitLfsPointer(handle.name) is False


def test_anUnreadableFileIsNotCalledAPointer() -> None:
    with tempfile.TemporaryDirectory() as folder:
        assert config._isGitLfsPointer(folder) is False


def test_startupLogsEachMissingEngineFile(caplog: pytest.LogCaptureFixture) -> None:
    with patch.object(main, "engineFileProblems", return_value=["GATE_WEIGHTS_PATH is unset."]), \
         caplog.at_level(logging.ERROR, logger="main"), \
         TestClient(main.app):
        pass

    assert [r.getMessage() for r in caplog.records if r.name == "main"] == [
        "AI engine: GATE_WEIGHTS_PATH is unset."
    ]


# ── The pinned engine: version and overrides ─────────────────────────────────

@pytest.mark.parametrize("version", ["0.2.0", "0.2.1", "0.10.0", "1.0.0", "0.3.0rc1"])
def test_theMinimumEngineOrNewerIsAccepted(version: str) -> None:
    with patch.object(config, "_installedEngineVersion", return_value=version):
        assert config._engineVersionProblems() == []


@pytest.mark.parametrize("version", ["0.1.0", "0.1.9", "0.0.1"])
def test_anOlderEngineIsReported(version: str) -> None:
    with patch.object(config, "_installedEngineVersion", return_value=version):
        [problem] = config._engineVersionProblems()
    assert problem.startswith(f"The installed AI engine is {version}; this backend needs 0.2.0 or newer.")
    assert "requirements.txt" in problem


def test_aMissingEngineIsReported() -> None:
    with patch.object(config, "_installedEngineVersion", return_value=None):
        [problem] = config._engineVersionProblems()
    assert "is not installed" in problem


def test_theRealInstalledEngineIsReadByItsDistributionName() -> None:
    # CI installs the pinned engine from requirements.txt.
    assert config._installedEngineVersion() is not None


def _bundledIn(packageDir: str):
    def find(*parts: str) -> str:
        path = Path(packageDir).joinpath(*parts)
        return str(path) if path.is_file() else ""
    return find


def test_aVariablePointingAwayFromTheEnginesOwnFileIsReported() -> None:
    with tempfile.TemporaryDirectory() as packageDir, tempfile.TemporaryDirectory() as elsewhere:
        _packageWith(packageDir, "models/yolov8/best.pt", "models/gate/gate.pt")
        stale = Path(elsewhere) / "weights.pt"
        stale.write_bytes(b"old model")
        bundledGate = str(Path(packageDir) / "models/gate/gate.pt")
        with patch.object(config, "_bundledEngineFile", side_effect=_bundledIn(packageDir)):
            problems = config._overrideProblems(_settingsWith(model=str(stale), gate=bundledGate))

    assert len(problems) == 1   # the gate path is the engine's own file
    assert problems[0].startswith(f"MODEL_WEIGHTS_PATH points to {stale}, not the installed engine's own file")
    assert "Remove the variable" in problems[0]


def test_aVariableNamingTheEnginesOwnFileIsFine() -> None:
    with tempfile.TemporaryDirectory() as packageDir:
        _packageWith(packageDir, "models/yolov8/best.pt")
        own = str(Path(packageDir) / "models" / "yolov8" / ".." / "yolov8" / "best.pt")
        with patch.object(config, "_bundledEngineFile", side_effect=_bundledIn(packageDir)):
            assert config._overrideProblems(_settingsWith(model=own)) == []


def test_withoutABundledFileAnOverrideIsLeftToTheFileChecks() -> None:
    with patch.object(config, "_bundledEngineFile", return_value=""):
        assert config._overrideProblems(_settingsWith(model="/somewhere/best.pt")) == []


def test_theVersionAndOverrideProblemsComeFirst() -> None:
    with patch.object(config, "_engineVersionProblems", return_value=["old engine"]), \
         patch.object(config, "_overrideProblems", return_value=["override"]), \
         patch.object(config, "_bundledEngineFile", return_value=""):
        problems = config.engineFileProblems(_settingsWith())
    assert problems[:2] == ["old engine", "override"] and len(problems) == 5


# ── Startup: the flag decides whether a problem stops the server ───────────────

def test_withTheFlagOnAnEngineProblemStopsTheServer(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.settings, "fieldWorkflowEnabled", True)
    with patch.object(main, "engineFileProblems", return_value=["MODEL_WEIGHTS_PATH points to /old/weights.pt"]), \
         caplog.at_level(logging.ERROR, logger="main"), \
         pytest.raises(RuntimeError, match="FIELD_WORKFLOW_ENABLED is on but the AI engine is not ready"), \
         TestClient(main.app):
        pass

    assert "AI engine: MODEL_WEIGHTS_PATH points to /old/weights.pt" in caplog.text


def test_withTheFlagOnAReadyEngineStarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "fieldWorkflowEnabled", True)
    with patch.object(main, "engineFileProblems", return_value=[]), TestClient(main.app) as client:
        assert client.get("/").status_code == 200


def test_withTheFlagOffAnEngineProblemIsOnlyLogged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "fieldWorkflowEnabled", False)
    with patch.object(main, "engineFileProblems", return_value=["The installed AI engine is 0.1.0"]), \
         TestClient(main.app) as client:
        assert client.get("/").status_code == 200
