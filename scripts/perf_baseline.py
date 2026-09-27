"""Load test for the mobile routes (UROLENS-220, SEC-4): p95 latency vs targets.

Runs against a deployed **staging** backend — never production: every call
here writes (image uploads, result rows, AI inference, and since UROLENS-222
an audit row per view). Run as a module from the repo root:

    PERF_MEDTECH_USERNAME=... PERF_MEDTECH_PASSWORD=... \\
    PERF_SUPERVISOR_USERNAME=... PERF_SUPERVISOR_PASSWORD=... \\
    PERF_UPLOAD_SPECIMEN_IDS=<uuid>[,<uuid>...] \\
    python -m scripts.perf_baseline --base-url https://staging.example.com \\
        --confirm-host staging.example.com --deployed-ref <branch-or-sha> \\
        --out docs/perf/baseline.json

- `--confirm-host` must equal the URL's host exactly, so a stray run can't
  hit the wrong server.
- Logs in once per role and reuses the token (login is rate limited to 5
  attempts / 5 min per account).
- Upload specimens must be assigned to the MedTech and have no submitted
  result; each is uploaded to repeatedly (each upload replaces the previous
  image). Upload concurrency is capped at the number of specimen IDs, since
  uploads to one specimen are serialized by its row lock.
- Credentials come from environment variables (test-harness secrets, not app
  config — hence not `src.core.config.settings`); missing ones are prompted.

Exit code is non-zero if any scenario misses its p95 target or errors.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import json
import math
import os
import random
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx
from PIL import Image

# Proposed p95 targets in ms (SEC-4) — see docs/performance-baseline-UROLENS-220.md
# for the reasoning. Upload includes AI inference, which dominates it.
P95_TARGETS_MS: dict[str, float] = {
    "sync_pull_full": 1500,
    "sync_pull_delta": 800,
    "medtech_pending_list": 500,
    "supervisor_pending_list": 500,
    "result_detail": 800,
    "image_upload": 8000,
}

_API = "/api/v1"
_WARMUP_REQUESTS = 3
_REQUEST_TIMEOUT_SECONDS = 60


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (`pct` in 0-100) of `values`; NaN if empty."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class ScenarioResult:
    """Latencies and outcomes of one scenario at one concurrency level."""

    name: str
    concurrency: int
    latenciesMs: list[float] = field(default_factory=list)
    statuses: Counter = field(default_factory=Counter)
    wallSeconds: float = 0.0

    def summary(self) -> dict:
        """Aggregate stats, plus the verdict against the scenario's p95 target."""
        ok = sum(n for s, n in self.statuses.items() if isinstance(s, int) and 200 <= s < 300)
        total = sum(self.statuses.values())
        p95 = percentile(self.latenciesMs, 95)
        target = P95_TARGETS_MS.get(self.name)
        return {
            "scenario": self.name,
            "concurrency": self.concurrency,
            "requests": total,
            "errors": total - ok,
            "statuses": {str(k): v for k, v in sorted(self.statuses.items(), key=str)},
            "p50_ms": round(percentile(self.latenciesMs, 50), 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(percentile(self.latenciesMs, 99), 1),
            "max_ms": round(max(self.latenciesMs), 1) if self.latenciesMs else math.nan,
            "throughput_rps": round(total / self.wallSeconds, 2) if self.wallSeconds else 0.0,
            "p95_target_ms": target,
            "passed": bool(target is not None and ok == total and total > 0 and p95 <= target),
        }


def requireHostConfirmation(baseUrl: str, confirmHost: str) -> str:
    """Return the URL's host, refusing to continue unless it matches `confirmHost`."""
    host = urlparse(baseUrl).hostname or ""
    if not host or host != confirmHost:
        raise SystemExit(
            f"Refusing to run: --confirm-host {confirmHost!r} doesn't match {host!r}. "
            "This test writes data — point it at staging, never production."
        )
    return host


def makeUploadImage(width: int = 2048, height: int = 1536) -> bytes:
    """A photo-sized JPEG (noisy, so it compresses like a real camera image)."""
    rng = random.Random(220)  # noqa: S311 - test data, not security
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def runScenario(
    name: str, concurrency: int, requests: int, send: Callable[[int], Awaitable[httpx.Response]]
) -> ScenarioResult:
    """Send `requests` calls with `concurrency` workers, after a short warm-up."""
    for i in range(_WARMUP_REQUESTS):
        await send(i)
    result = ScenarioResult(name=name, concurrency=concurrency)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(requests):
        queue.put_nowait(i)

    async def worker() -> None:
        while not queue.empty():
            i = queue.get_nowait()
            started = time.perf_counter()
            try:
                response = await send(i)
                result.statuses[response.status_code] += 1
            except httpx.HTTPError as exc:
                result.statuses[type(exc).__name__] += 1
            result.latenciesMs.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    result.wallSeconds = time.perf_counter() - started
    return result


def _credential(name: str, secret: bool) -> str:
    value = os.environ.get(name)
    if value:
        return value
    return getpass.getpass(f"{name}: ") if secret else input(f"{name}: ")


async def _login(client: httpx.AsyncClient, role: str) -> dict[str, str]:
    response = await client.post(
        f"{_API}/auth/login",
        json={
            "username": _credential(f"PERF_{role}_USERNAME", secret=False),
            "password": _credential(f"PERF_{role}_PASSWORD", secret=True),
        },
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


async def runBaseline(args: argparse.Namespace) -> list[dict]:
    """Log in, run every scenario at each concurrency level, return summaries."""
    summaries: list[dict] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        medtech = await _login(client, "MEDTECH")
        supervisor = await _login(client, "SUPERVISOR")
        since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

        reads: dict[str, Callable[[int], Awaitable[httpx.Response]]] = {
            "sync_pull_full": lambda _: client.get(f"{_API}/sync/pull", headers=medtech),
            "sync_pull_delta": lambda _: client.get(
                f"{_API}/sync/pull", params={"lastSyncedAt": since}, headers=medtech
            ),
            "medtech_pending_list": lambda _: client.get(
                f"{_API}/results/medtech/pending", params={"pageSize": 20}, headers=medtech
            ),
            "supervisor_pending_list": lambda _: client.get(
                f"{_API}/results/pending", params={"pageSize": 20}, headers=supervisor
            ),
        }
        pending = (await reads["supervisor_pending_list"](0)).json().get("items", [])
        if pending:
            resultId = pending[0]["resultId"]
            reads["result_detail"] = lambda _: client.get(f"{_API}/results/{resultId}", headers=supervisor)
        else:
            print("! result_detail skipped: the supervisor has no pending results", file=sys.stderr)

        for name, send in reads.items():
            for concurrency in args.concurrency:
                result = await runScenario(name, concurrency, args.requests, send)
                summaries.append(result.summary())
                _printRow(summaries[-1])

        specimenIds = [s for s in os.environ.get("PERF_UPLOAD_SPECIMEN_IDS", "").split(",") if s]
        if specimenIds:
            image = makeUploadImage()

            def upload(i: int) -> Awaitable[httpx.Response]:
                return client.post(
                    f"{_API}/images/upload",
                    data={"specimen_id": specimenIds[i % len(specimenIds)]},
                    files={"file": ("perf.jpg", image, "image/jpeg")},
                    headers=medtech,
                )

            for concurrency in sorted({1, min(len(specimenIds), max(args.concurrency))}):
                result = await runScenario("image_upload", concurrency, args.upload_requests, upload)
                summaries.append(result.summary())
                _printRow(summaries[-1])
        else:
            print("! image_upload skipped: set PERF_UPLOAD_SPECIMEN_IDS", file=sys.stderr)
    return summaries


def _printRow(s: dict) -> None:
    verdict = "PASS" if s["passed"] else "FAIL"
    print(
        f"{s['scenario']:<24} c={s['concurrency']:<3} n={s['requests']:<4} err={s['errors']:<3} "
        f"p50={s['p50_ms']:>8.1f} p95={s['p95_ms']:>8.1f} p99={s['p99_ms']:>8.1f} "
        f"target={s['p95_target_ms']:>6} {verdict}"
    )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the baseline, write the JSON report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--confirm-host", required=True)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument("--requests", type=int, default=100, help="per read scenario and level")
    parser.add_argument("--upload-requests", type=int, default=20)
    parser.add_argument(
        "--deployed-ref", required=True,
        help="the branch/commit staging is running — recorded so the baseline says what was measured",
    )
    parser.add_argument("--out", help="write the JSON report here")
    args = parser.parse_args(argv)
    host = requireHostConfirmation(args.base_url, args.confirm_host)

    summaries = asyncio.run(runBaseline(args))
    report = {
        "ticket": "UROLENS-220 SEC-4",
        "host": host,
        "deployed_ref": args.deployed_ref,
        "measured_at": datetime.now(UTC).isoformat(),
        "p95_targets_ms": P95_TARGETS_MS,
        "results": summaries,
    }
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"report written to {args.out}")
    return 0 if summaries and all(s["passed"] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
