# Backend Consolidation Plan

Status: **audit complete — awaiting review before any deletion/merge.** Do not delete,
unmount, or merge anything against this doc until it's been reviewed. This document
is Step 1 of the required process; domain-by-domain merging is future work gated on
this review.

Two independent, security-relevant fixes with no dependency on the decisions below
(CORS `allow_credentials`, hardcoded JWT signing-key fallback) were pulled out and
shipped separately — see `changelog.md` and PR `fix/cors-jwt-secret-hardening`.
Not covered further here.

## Scope rule for every merge below

**Routers stay thin — no direct Supabase/DB call, and no business logic, in a router
function. Service layer only.** This is the fix for "one DB access pattern" at the
router level, independent of which client (SQLAlchemy vs Supabase) a service ends up
using internally. It applies retroactively to every router touched in every step
below — most acutely in the image/AI-analysis merge (Step 4), where the currently
*live* router (`src/urolens/api/image.py`) is the worst offender: it contains ~250
lines of inline Supabase calls, retry logic, and AI-inference orchestration that
belong in a service class, not the route handler.

## Global architecture decisions

These are engineering-judgment calls, not product decisions — made and documented
here per the task brief's instruction to "state reasoning either way," not deferred
to another round of questions.

- **DB access pattern: SQLAlchemy `AsyncSession` (`src/urolens/core/database.py`) is
  the primary pattern for all relational domain services going forward.** Reasoning:
  the highest-stakes workflow in this codebase — result confirmation
  (`ResultConfirmationService.confirm_result`) — already depends on SQLAlchemy's
  SAVEPOINT/transaction semantics to isolate the Smart Diagnosis engine call and
  turn a unique-constraint race into a clean 409. That correctness property doesn't
  exist in the Supabase-REST call pattern used everywhere else, and transactional
  integrity matters more here than ORM ergonomics or avoiding a migration cost.
  Every "web" router still hitting Supabase directly for table reads/writes
  (patients, queue, result_releasing, patient_portal, intake, specimens,
  lab-requests, labeling) needs to move to SQLAlchemy models as its domain gets
  merged — this is called out per-domain below, not done in one big-bang pass.
- **Object storage: Supabase Storage, not S3/boto3.** Confirmed via `.env.example`
  and `requirements.txt` — no AWS credentials exist anywhere in this project.
  `boto3`/`S3_BUCKET` code (`src/urolens/services/ai_integration.py`,
  `ai_integration_service.py`) has never been wired to real infrastructure; Supabase
  Storage (`SUPABASE_IMAGE_BUCKET`, used by the live `src/urolens/api/image.py`) is
  the only backend actually in use. The canonical image service (Step 4) is built on
  Supabase Storage; `boto3`/`botocore`/`s3transfer` get removed from
  `requirements.txt` once nothing references them (standards rule 16).
- **`UserRole` enum: keep `src/urolens/core/enums.py` (`StrEnum`), delete
  `src/urolens/models/user.py`'s `UserRole` (`str, Enum`).** Same members; the
  `core/enums.py` version is what the already-correctly-authenticated routers
  (`patients.py`, `queue.py`, `result_releasing.py`, `patient_portal.py`) import.
  `models/user.py` keeps its `User` SQLAlchemy model; only the duplicate enum inside
  it is removed, with call sites re-pointed to `core.enums.UserRole`.
- **Canonical auth/RBAC dependency: `app.middleware.rbac.RequireRole` /
  `get_current_user` (session-revocation-aware, audit-logs denials via
  `app.services.audit_logger`).** Not `src/urolens/middleware/rbac.py` — that module
  only decodes the JWT signature and never checks `is_session_active`, so a revoked
  session's token still works. This is a correction to how the original task brief
  described the split: the brief attributed the "used correctly by patients.py /
  queue.py" pattern to a module under `src/urolens/`, but reading the actual imports
  shows the session-checked implementation lives in `app/middleware/rbac.py`, and
  `patients.py`/`queue.py`/`result_releasing.py`/`patient_portal.py` already import
  it from there — they just pair it with `UserRole` from `src.urolens.core.enums`.
  End state: `app/middleware/rbac.py` becomes the one auth module (physical location
  can move during the auth/RBAC merge step, e.g. to `src/urolens/core/rbac.py`, but
  its logic — not `src/urolens/middleware/rbac.py`'s — is what survives).
  `src/urolens/middleware/rbac.py` is deleted once every importer is repointed.

## Duplicate pairs and decisions

| # | Feature | Keep | Delete | Notes |
|---|---|---|---|---|
| 1 | Auth/RBAC dependency | `app/middleware/rbac.py` (`RequireRole`, `get_current_user`) | `src/urolens/middleware/rbac.py` | See "Global architecture decisions" above — corrects the task brief's attribution. Every router currently importing from `src.urolens.middleware.rbac` (`notifications.py`, unmounted `src/urolens/api/results.py`, **and the currently-mounted `src/urolens/api/image.py`**) needs its import swapped as part of this merge, not just the two obviously-named modules. |
| 2 | `UserRole` enum | `src/urolens/core/enums.py` | `src/urolens/models/user.py`'s `UserRole` class (keep the `User` table model in that file) | Both confirmed read; same members, different base (`StrEnum` vs `str, enum.Enum`) — functionally interchangeable, so the choice is "whichever the already-correct routers use," per #1. |
| 3 | Create patient | `src/urolens/api/patients.py` + `PatientService` (`src/urolens/services/patient_service.py`) | `src/urolens/domains/intake/router.py`'s `register_patient_endpoint` + `search_patients_endpoint` | **Live PHI exposure, not just dead code**: the delete-candidate writes `first_name`/`last_name`/`date_of_birth` **unencrypted** with hardcoded `REAL_USER_ID` and no auth, into the *same* `patients` table `PatientService` writes to encrypted. Both are reachable today at different URL prefixes (`/api/v1/intake/patients` vs `/api/v1/patients`), so the table can already contain a mix of encrypted and plaintext PII rows — consistent with the silent `except: first = ""` fallback both search paths use on decrypt failure. Flag this as the most urgent finding in this table, on the same footing as rows 9–10 below (specimens receiving / labeling — the other live-PHI, unauthenticated findings; row 7 is a sizing note about the supervisor-review workflow, not an urgency comparison — corrected cross-reference). Residual issue in the *keeper*: `PatientService._generate_patient_uid` computes `max(existing)+1` with no re-check before insert — not concurrency-safe; needs a one-line uniqueness-retry fix (standards rule 5), not a redesign, during this merge. |
| 4 | AI integration / inference service | Rebuild: new service class modeled on `ai_integration.py`'s shape (proper class, not inline router logic), rewired onto Supabase Storage (per "Global architecture decisions"), with `src/urolens/api/image.py`'s router-inline production logic ported in (dash→underscore particle-class normalization, retry-without-`updated_at`, smart-diagnosis-at-upload precompute) | `ai_integration_service.py` outright; the inline logic currently in `src/urolens/api/image.py` (extracted into the new service, not left in the router — see scope rule above) | None of the three existing implementations survives unmodified — see full reasoning below. |
| 5 | Discard/retake image | `src/urolens/api/image.py` (once de-inlined per scope rule) + `ImageRetakeService` | `app/api/images.py`, `app/schemas/images.py`, `app/services/image_service.py` | Confirmed already unreachable — not imported by `main.py`. Also: `image.py` currently imports `RequireRole`/`UserRole` from the non-canonical pair (`src.urolens.middleware.rbac`, `models.user.UserRole`) — swap to the canonical pair as part of this merge (see #1). |
| 6 | Confirm / override results | `src/urolens/api/results.py` (currently commented out in `main.py`) + `ManualOverrideService`'s re-derivation logic | `app/api/results.py` (confirm/override routes only — see #7 for the rest of that file) + `app/services/result_service.py` | Confirmed: `app/services/result_service.py:136,171` takes `original_ai_value` as a caller-supplied argument and stores it as-is. `ManualOverrideService._extract_original_value` re-derives it from stored `ai_findings`, ignoring the caller's value. `src/urolens/api/results.py` also currently imports the non-canonical RBAC/UserRole pair — swap during this merge. |
| 7 | **Supervisor review/approval workflow — not in the original brief's table** | Needs to be *ported*, not picked between — no SQLAlchemy equivalent exists | `app/services/result_review_service.py` (636 lines: `get_pending`, `get_approved_today`, `get_escalated`, `save_annotation`, `approve_result`, `return_result`, `escalate_result`) is live behind `app/api/results.py`, pure Supabase REST, and has **no counterpart anywhere under `src/urolens/services/`.** This is a Tier-1 workflow (approve/return/escalate is the standards skill's own named example of the confirm→override→approve→release chain) that has to be rewritten against SQLAlchemy as part of the results merge (Step 5), not deleted or kept-as-is. Size this as its own line item in that step's estimate — it's the largest single piece of work in the results domain, larger than #6. |
| 8 | Result releasing / notifications | `src/urolens/api/result_releasing.py` + `ResultReleasingService`, `src/urolens/api/notifications.py` (once repointed to canonical RBAC/UserRole, #1/#2) + `NotificationService` | *(none found — brief's assumption holds)* | No legacy `app/`-tree releasing or notification service exists to compare against; nothing to delete here. `notifications.py` still needs its auth-import swap per #1. |
| 9 | Specimens: receive | `app/api/specimens.py` + `app/services/specimen_service.py` (`reject_specimen` only — real auth, ownership check) as the auth/service pattern to extend | `src/urolens/domains/intake/specimens_router.py` (`receive_specimen_endpoint`, `list_specimens_endpoint`, `search_pending_lab_requests`) | **No auth dependency on any of these three routes at all** — not even a weak one. Hardcoded `CURRENT_RECEPTIONIST_ID`. Writes plaintext `patient_name` (denormalized onto `specimens`) with no `encrypt_pii` call, while the labeling router's reads try `decrypt_pii` with a silent fallback — same encrypted/plaintext data-hazard pattern as patients (#3). `sample_uid` generated as `SMP-2026-{random 5-digit}` with no uniqueness check (rule 5). Per the AskUserQuestion answer, this stays documented here as the top-priority item in this domain's merge rather than an immediate separate fix — full rewiring (real auth, encryption, service-layer extraction, ID generation) is bigger than a single-file fix. |
| 10 | Specimens: labeling | *(new service to be written against SQLAlchemy `Specimen`/label models as part of this merge — no existing implementation is safe to keep as-is)* | `src/urolens/domains/intake/labeling_router.py` in its entirety | **This is the most severe finding in the audit.** `generate_specimen_label_endpoint` authenticates via `get_current_user_id`, which reads an `X-User-Id` **request header** supplied by the caller, falling back to a hardcoded UUID if absent — this is the standards skill's own named example of a forbidden pattern ("no client-supplied identity header trusted as auth, ever"). `confirm_label_affixed_endpoint` has **no auth dependency at all**, not even the fake header-based one, and can transition a specimen to `LABELED` and write label/print-job records unauthenticated. `search_received_specimens` is also unauthenticated. All three routes are reachable today (mounted via `labeling_router` in `main.py`). |
| 11 | Lab requests | *(new service to be written against SQLAlchemy as part of this merge)* | `src/urolens/domains/request/lab_requests_router.py` in its entirety | Same pattern as #9/#10: `create_lab_request_endpoint` and `get_physicians_endpoint` have no auth dependency, hardcoded `CURRENT_ENCODER_ID`, `request_uid` generated with the same uncollision-checked random-suffix pattern. Also contains a vestigial, unused `PHYSICIAN_UUID_MAP` dict hardcoding three UUIDs in source (one of them not even valid hex — `"f4e3d2c1-b0a9-8m7n-6p5q-4r3s2t1u0v9w"` contains `m`/`n`/`p`/`q`) with a comment telling a future dev to replace them — dead, misleading, and itself a rule-5-adjacent hazard; delete outright, nothing references it. |

## Merge order (unchanged from the task brief, confirmed still correct)

1. **Specimens / labeling / lab-requests** (rows 9–11) — now confirmed to be the
   highest-severity domain by auth-gap count, not just first by convention. Do this
   first both because it's earliest in the dependency chain and because it's where
   the worst live exposure is.
2. **Lab requests** — folded into the same pass as specimens above; they share the
   same auth/ID-generation defects and the same target pattern.
3. **Patients** (row 3).
4. **Image / AI analysis** (rows 4–5).
5. **Results confirm/override/approve/release** (rows 6–8) — largest single domain
   by LOC once the supervisor-approval port (row 7) is counted.
6. **Notifications** — folded into step 5's merge since `notifications.py`'s only
   outstanding issue is the auth-import swap (row 1/8).
7. **Auth/RBAC** (row 1) — last, as originally planned: every other domain above
   should already be calling the canonical `app.middleware.rbac` dependency by the
   time this step formalizes it as the single auth module and deletes
   `src/urolens/middleware/rbac.py`.

After each step: run the existing test suite, fix/replace any test whose
constructor/signature no longer matches, confirm the app still boots
(`python -c "import main"` — used for the CORS/JWT fix above, works) and that the
OpenAPI schema still generates cleanly, before starting the next domain.

## Alembic

Two heads confirmed today by reading every migration's `revision`/`down_revision`
directly (the `alembic` CLI itself won't invoke in this shell — `python -m alembic`
reports "No module named alembic.__main__"; a venv/entry-point issue, not
investigated further since reading the files directly was sufficient and more
precise — whoever runs the actual merge should confirm with a working `alembic
heads` before authoring the merge revision):

- **Branch A** (dead-ends at `0011`): `0005 → 0010 (queue_assignments) → 0011
  (notifications)`. Nothing declares `0011` as its `down_revision`.
- **Branch B** (dead-ends at `0030`): `0005 → 0013 → 0014 → 0015
  (smart_diagnosis_outputs) → 0016 (manual_overrides) → 0017 → 0018 → 0019 → 0020 →
  0021 → 0027 → 0028 → 0025 → 0026 → 0029 → 0030`.

A merge revision joining `0011` and `0030` is required once the auth/RBAC merge
(the last domain step) is otherwise done, per the task brief's instruction to do
this "once merged."

Two **independent filename/revision-ID mismatches** (standards rule 13), unrelated
to the two-head issue, found while building the above chain and worth fixing in the
same pass:
- `0015_analysis_results_add_patient_portal_fields.py` embeds `revision = "0027"`
  (not `"0015"`) — its filename collides with the unrelated, correctly-numbered
  `0015_smart_diagnosis_outputs.py`.
- `0016_patients_add_user_id.py` embeds `revision = "0028"` (not `"0016"`) — same
  collision pattern against `0016_manual_overrides.py`.

Rename both files to match their embedded revision IDs (`0027_...py`,
`0028_...py`) as part of whichever merge step touches them.

## Empty stub packages

`src/urolens/domains/{diagnostics,distribution,tracking}/{models,router,service}.py`
are confirmed 0 bytes each, and none of the three package names appears anywhere in
`main.py`. Delete all three packages outright — nothing in this consolidation needs
them, and no functionality is lost.

Also: `src/urolens/domains/request/` has no `__init__.py` (every other domain
package does). Minor package-boundary inconsistency (standards rules 6/7) — fix
during the specimens/lab-requests merge (step 1 above), not urgent standalone.

## Mobile-only surface

`app/api/sync.py` is read-only today (`GET /pull`, cursor via `last_synced_at`) —
there is no offline batch-write/push endpoint anywhere in the codebase. The task
brief's mention of a "mobile-only offline batch-sync endpoint" describes a
hypothetical future need, not something currently duplicated or in conflict with the
web routers. Stated here rather than guessed at: **if an offline-write sync
capability is actually required, that's a product decision that needs to be made
explicitly before the results/patients merges land**, since it would change what
"one service layer per domain" needs to support (conflict resolution on writes made
offline). Not blocking this plan — no such requirement exists in code today to
reconcile.

## Verified-clean items (checked, not re-checked a third time)

- **RLS**: every migration file matching an RLS-related grep
  (`0004_patients.py`, `0005_consents.py`, `0010_queue_assignments.py`,
  `0011_notifications.py`, `0015_analysis_results_add_patient_portal_fields.py`,
  `0025_result_views.py`, `0026_result_releases.py`, `0030_result_retrievals.py`)
  was identified by filename in this audit. Per-table verification that RLS-disabled
  is actually compensated for by the auth layer (per the task brief's explicit
  instruction to verify, not assume) will happen naturally as each table's owning
  domain gets merged onto the canonical auth dependency (step 1 above, in
  particular) — by definition, a table whose only writers are still on the
  unauthenticated intake/specimens/labeling routers (rows 9–11) does **not**
  currently have that compensation, which is exactly why those rows are the
  top-priority merge.
- **`print()` / `# REMOVE` markers**: grepped across the full repo. All hits are in
  `seed_users.py`, `seed_results.py`, `seed_specimens.py`, `test_image_upload.py`,
  `app/services/patient_auth_service.py`, and `app/services/audit_logger.py` — none
  in a router or domain service file that ships to production. Clean.
- **Hardcoded actor/user UUIDs**: same grep pass. Every hit already appears in the
  duplicate-pairs table above (`REAL_USER_ID`, `CURRENT_RECEPTIONIST_ID`,
  `CURRENT_ENCODER_ID`, the `PHYSICIAN_UUID_MAP`) — all confined to the
  delete-candidate files in rows 3, 9, 11. Nothing hardcoded survives in any of the
  "keep" column.

## Config/secrets note (beyond the two issues already fixed separately)

Both `app/config.py` and `src/urolens/core/config.py` are flat `os.getenv` module
constants, not a `Settings` class — standards rule 1 isn't fully met by either even
after the JWT fix. `src/urolens/core/config.py`'s `ENCRYPTION_KEY` equivalent isn't
even present as a constant there (only in `app/config.py`, defaulting to `""` — an
empty Fernet key, which will fail at first use rather than at startup). Consolidating
both into one `Settings` class with startup validation for every secret
(`JWT_SIGNING_KEY`, `ENCRYPTION_KEY`, DB creds) is real work that belongs in the
auth/RBAC merge (the last step), where the two config modules get unified anyway —
not attempted piecemeal here.
