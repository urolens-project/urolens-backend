# Performance baseline — mobile routes (UROLENS-220, SEC-4)

| | |
|---|---|
| **Ticket** | UROLENS-220 "Mobile" (subtask of UROLENS-80 *[Security & Compliance] Supabase Integration*) — "performance testing" |
| **Scope** | The backend routes the MedTech mobile app depends on, plus the supervisor lists/detail that read the same data |
| **Tool** | [`scripts/perf_baseline.py`](../scripts/perf_baseline.py) |
| **Status** | Targets proposed; **baseline not yet recorded** — waiting on a staging run |

## Why staging, not local or production

- **Not production:** every measured call writes — uploads store an image, run AI
  inference and reset a result; since UROLENS-222 every view of patient data writes
  an audit row. A load test there would pollute patient records and the RA 10173
  audit trail.
- **Not local:** every authenticated request checks its session through Supabase
  REST, and `/sync/pull` reads entirely through it, so a local run can't include
  the network hops that dominate real latency; AI inference also runs very
  differently on a laptop than on the server.

## Scenarios

| Scenario | Route | As | Notes |
|---|---|---|---|
| `sync_pull_full` | `GET /api/v1/sync/pull` | MedTech | All of the MedTech's specimens, assignments, results |
| `sync_pull_delta` | `GET /api/v1/sync/pull?lastSyncedAt=<now−5 min>` | MedTech | The common case on a running device |
| `medtech_pending_list` | `GET /api/v1/results/medtech/pending?pageSize=20` | MedTech | Logs `PENDING_RESULTS_VIEWED` |
| `supervisor_pending_list` | `GET /api/v1/results/pending?pageSize=20` | Supervisor | |
| `result_detail` | `GET /api/v1/results/{id}` | Supervisor | Decrypts patient fields, signs the image URL, logs `RESULT_DETAIL_VIEWED` |
| `image_upload` | `POST /api/v1/images/upload` | MedTech | 2048×1536 JPEG (~2.9 MB) — **includes AI inference** |

Reads run at concurrency 1, 5 and 10 (100 measured requests each, after 3
warm-up requests). Uploads run at concurrency 1 and up to the number of
upload specimens (20 requests), because uploads to one specimen are serialized
by its row lock (SEC-2).

## Proposed p95 targets

| Scenario | p95 target | Reasoning |
|---|---|---|
| `sync_pull_delta` | **800 ms** | Runs in the background on every app resume; must feel instant on lab Wi-Fi |
| `sync_pull_full` | **1500 ms** | First login / reinstall only; one MedTech's working set |
| `medtech_pending_list` | **500 ms** | Interactive list; two small queries + an audit row |
| `supervisor_pending_list` | **500 ms** | Interactive list |
| `result_detail` | **800 ms** | Interactive; decrypts PII and signs the image URL |
| `image_upload` | **8000 ms** | Dominated by YOLOv8 inference; the MedTech waits on a progress screen |

These are proposals for the team to confirm; the tool fails a scenario that
misses its target **or** returns any error.

## How to run

Prerequisites on the staging environment:
- a MedTech and a Supervisor **test** account (not real staff);
- 2–3 specimens assigned to that MedTech with no submitted result (for uploads);
- the MedTech has some specimens/results so sync and lists return data;
- staging uses **its own database** — not the production Supabase project.

```bash
PERF_MEDTECH_USERNAME=... PERF_MEDTECH_PASSWORD=... \
PERF_SUPERVISOR_USERNAME=... PERF_SUPERVISOR_PASSWORD=... \
PERF_UPLOAD_SPECIMEN_IDS=<uuid>,<uuid> \
python -m scripts.perf_baseline \
  --base-url https://<staging-host> --confirm-host <staging-host> \
  --deployed-ref <branch-or-sha staging runs> \
  --out docs/perf/baseline-<date>.json
```

Missing credentials are prompted (hidden). `--confirm-host` must equal the URL's
host or the tool refuses to start.

## Baseline results

*Not yet recorded.* To be filled from the staging run: environment (host, deployed
ref, instance size, worker count), then per scenario and concurrency level the
requests, errors, p50 / p95 / p99 / max and throughput, with pass/fail against the
targets above.

## Known factors

- Every authenticated request makes one Supabase REST call (session check) before
  any work — a fixed network cost on every route.
- `sync_pull` makes 3 Supabase REST calls plus, since UROLENS-222, an audit insert
  and commit.
- Upload holds the specimen's row lock for the whole request, including storage
  upload and inference (SEC-2) — concurrent uploads to *one* specimen queue.
- The CI install currently pulls unpinned `torch`/`ultralytics` (audit F-12), so the
  inference library version on staging may differ between deploys; record it.
