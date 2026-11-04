"""`GET /health/ready`, the CORS origin setting, and the startup engine preload."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from src.core import engine_preload

# The suite-wide fixture replaces this per test; keep the real one.
_realPreload = engine_preload.preloadEngine


@pytest.fixture
def modelReady(monkeypatch: pytest.MonkeyPatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(engine_preload, "_modelReady", value)

    return _set


def _client() -> TestClient:
    return TestClient(main.app)


# ── /health/ready ────────────────────────────────────────────────────────────

def test_readyWhenDatabaseAndModelAreUp(modelReady) -> None:
    modelReady(True)
    with patch.object(main, "_dbReachable", return_value=True), _client() as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"db": True, "model": True}


def test_notReadyWhenTheDatabaseIsDown(modelReady) -> None:
    modelReady(True)
    with patch.object(main, "_dbReachable", return_value=False), _client() as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"db": False, "model": True}


def test_notReadyWhenTheModelIsNotLoaded(modelReady) -> None:
    modelReady(False)
    with patch.object(main, "_dbReachable", return_value=True), _client() as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"db": True, "model": False}


def test_bodyHasExactlyTheTwoKeys(modelReady) -> None:
    modelReady(False)
    with patch.object(main, "_dbReachable", return_value=False), _client() as client:
        body = client.get("/health/ready").json()
    assert set(body) == {"db", "model"}


def test_aRealDatabaseFailureIsReportedNotRaised(modelReady) -> None:
    modelReady(True)
    broken = MagicMock(side_effect=OSError("connection refused"))
    with patch.object(main, "AsyncSessionLocal", broken), _client() as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"db": False, "model": True}


def test_aSlowDatabaseIsGivenUpOnAfterTheTimeout(modelReady, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class SlowSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> None:
            await asyncio.sleep(5)

    modelReady(True)
    monkeypatch.setattr(main, "_DB_PROBE_TIMEOUT_SECONDS", 0.05)
    with patch.object(main, "AsyncSessionLocal", SlowSession), _client() as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"db": False, "model": True}


def test_rootHealthCheckIsUnchanged() -> None:
    with _client() as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "UroLens Core Platform Architecture"}


# ── CORS_ALLOWED_ORIGINS ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("https://app.example.com", ["https://app.example.com"]),
        (
            "https://a.example.com,https://b.example.com",
            ["https://a.example.com", "https://b.example.com"],
        ),
        (
            "  https://a.example.com ,\thttps://b.example.com  , ",
            ["https://a.example.com", "https://b.example.com"],
        ),
        ("https://a.example.com,,", ["https://a.example.com"]),
    ],
)
def test_corsOriginsAreParsedFromTheEnvValue(raw: str | None, expected: list[str]) -> None:
    assert main.parseCorsOrigins(raw) == expected


def test_theAppNoLongerAllowsEveryOrigin() -> None:
    cors = next(m for m in main.app.user_middleware if m.cls.__name__ == "CORSMiddleware")
    assert "*" not in cors.kwargs["allow_origins"]


# ── Engine preload ───────────────────────────────────────────────────────────

def test_preloadLoadsTheEngineAndWarmsItOnABlankImage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_preload, "_modelReady", False)
    engine, gate = MagicMock(), MagicMock()
    with patch("urolens_ai.inference.yolo_engine.get_engine", return_value=engine), \
         patch("urolens_ai.inference.input_gate.get_gate", return_value=gate):
        assert _realPreload() is True

    blank = engine.run.call_args.args[0]
    assert blank.shape == (480, 640, 3) and blank.dtype == np.float32 and not blank.any()
    gate.slide_probability.assert_called_once()
    assert engine_preload.isModelReady() is True


def test_preloadFailureIsLoggedAndDoesNotRaise(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_preload, "_modelReady", True)
    with patch("urolens_ai.inference.yolo_engine.get_engine", side_effect=RuntimeError("no weights")), \
         caplog.at_level(logging.ERROR, logger="src.core.engine_preload"):
        assert _realPreload() is False

    assert "preload failed" in caplog.text
    assert engine_preload.isModelReady() is False


def test_theAppStartsAndReportsNotReadyWhenPreloadFails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_preload, "_modelReady", False)
    monkeypatch.setattr(engine_preload, "preloadEngine", _realPreload)
    with patch("urolens_ai.inference.yolo_engine.get_engine", side_effect=RuntimeError("no weights")), \
         patch.object(main, "_dbReachable", return_value=True), \
         _client() as client:
        assert client.get("/").status_code == 200
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"db": True, "model": False}


@pytest.mark.parametrize(("env", "expected"), [(None, 1), ("", 1), ("4", 4), (" 2 ", 2), ("0", 1), ("x", 1)])
def test_torchThreadsDefaultToOne(env: str | None, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    if env is None:
        monkeypatch.delenv("TORCH_NUM_THREADS", raising=False)
    else:
        monkeypatch.setenv("TORCH_NUM_THREADS", env)
    assert engine_preload._torchThreads() == expected
