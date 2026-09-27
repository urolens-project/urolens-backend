"""Unit tests — scripts/perf_baseline.py (UROLENS-220, SEC-4 load-test tool).

The maths and guard rails, not a load test: nearest-rank percentiles, the
p95 verdict (errors never pass), the host-confirmation guard, the upload
image, and the scenario runner (warm-up excluded, concurrency honoured).
"""
from __future__ import annotations

import asyncio
import math
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from scripts import perf_baseline
from scripts.perf_baseline import (
    P95_TARGETS_MS,
    ScenarioResult,
    makeUploadImage,
    percentile,
    requireHostConfirmation,
    runScenario,
)


def test_percentileUsesNearestRank():
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([7.0], 95) == 7.0
    assert math.isnan(percentile([], 95))


def test_summaryPassesOnlyWhenP95IsWithinTargetAndNothingFailed():
    within = ScenarioResult("medtech_pending_list", 5, [100.0] * 19 + [400.0], Counter({200: 20}), 2.0)
    assert within.summary()["passed"] is True
    assert within.summary()["throughput_rps"] == 10.0

    slow = ScenarioResult("medtech_pending_list", 5, [900.0] * 20, Counter({200: 20}), 2.0)
    assert slow.summary()["passed"] is False


def test_anyErrorFailsTheScenarioEvenIfFast():
    result = ScenarioResult("sync_pull_delta", 1, [10.0] * 20, Counter({200: 19, 500: 1}), 1.0)
    summary = result.summary()
    assert summary["errors"] == 1
    assert summary["passed"] is False


def test_everyScenarioHasATarget():
    assert set(P95_TARGETS_MS) == {
        "sync_pull_full", "sync_pull_delta", "medtech_pending_list",
        "supervisor_pending_list", "result_detail", "image_upload",
    }


def test_hostGuardRefusesAMismatchedHost():
    with pytest.raises(SystemExit, match="Refusing to run"):
        requireHostConfirmation("https://urolens-api.example.com", "staging.example.com")


def test_hostGuardAcceptsTheConfirmedHost():
    assert requireHostConfirmation("https://staging.example.com/", "staging.example.com") == "staging.example.com"


def test_uploadImageIsAPhotoSizedJpegUnderTheUploadCap():
    image = makeUploadImage()
    assert image[:3] == b"\xff\xd8\xff"  # JPEG
    assert 1 * 1024 * 1024 < len(image) < 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_runScenarioExcludesWarmupAndHonoursConcurrency():
    inFlight = 0
    peak = 0
    calls = 0

    async def send(_: int) -> httpx.Response:
        nonlocal inFlight, peak, calls
        calls += 1
        inFlight += 1
        peak = max(peak, inFlight)
        await asyncio.sleep(0.001)
        inFlight -= 1
        return httpx.Response(200)

    result = await runScenario("sync_pull_full", concurrency=4, requests=20, send=send)

    assert calls == 23  # 3 warm-up + 20 measured
    assert len(result.latenciesMs) == 20
    assert result.statuses == Counter({200: 20})
    assert peak == 4


@pytest.mark.asyncio
async def test_runScenarioRecordsTransportErrorsInsteadOfCrashing():
    async def send(i: int) -> httpx.Response:
        if i >= 3 and i % 2:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(200)

    result = await runScenario("sync_pull_full", concurrency=1, requests=4, send=send)

    assert sum(result.statuses.values()) == 4
    assert result.statuses["ConnectTimeout"] >= 1


def test_reportFolderIsCreatedIfMissing():
    # docs/perf/ isn't tracked by git until the first report lands in it.
    async def _fakeRun(args):
        return [{"passed": True}]

    with tempfile.TemporaryDirectory() as tmp, \
         patch.object(perf_baseline, "runBaseline", _fakeRun):
        out = Path(tmp) / "perf" / "nested" / "report.json"
        code = perf_baseline.main([
            "--base-url", "https://staging.example.com", "--confirm-host", "staging.example.com",
            "--deployed-ref", "test", "--out", str(out),
        ])

        assert code == 0
        assert out.exists()
