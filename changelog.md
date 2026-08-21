# Changelog

## Unreleased

### Fixed
- **CORS misconfiguration**: `main.py` combined `allow_origins=["*"]` with
  `allow_credentials=True`, which lets any origin read authenticated responses made
  with the browser's ambient credentials. The API only authenticates via a Bearer
  token in the `Authorization` header (never cookies — confirmed no
  `withCredentials`/`credentials: include` anywhere in `urolens-web`), so credentialed
  CORS was never needed. Set `allow_credentials=False`.
- **Hardcoded JWT signing-key fallback**: both `app/config.py` and
  `src/urolens/core/config.py` defaulted `JWT_SIGNING_KEY` to the literal string
  `"change-me-in-production-use-a-long-random-string"` if the env var was unset, so a
  misconfigured deployment would silently sign tokens with a public, guessable key.
  Both modules now raise `RuntimeError` at import time if `JWT_SIGNING_KEY` is unset
  or still equal to that placeholder.

These two fixes were pulled out of the in-progress backend consolidation
(`docs/backend-consolidation-plan.md`) and shipped independently since they're
single-file, don't depend on any of the consolidation's open decisions (which
service layer survives, which RBAC import wins), and were live security gaps.

## Track A2 — minimal PHI auth patch (rows 9–11 of the consolidation plan)

### Fixed
- **Auth added to three previously-unauthenticated routers**: `receive_specimen_endpoint`,
  `list_specimens_endpoint`, and `search_pending_lab_requests` in
  `src/urolens/domains/intake/specimens_router.py`; `generate_specimen_label_endpoint`,
  `confirm_label_affixed_endpoint`, and `search_received_specimens` in
  `src/urolens/domains/intake/labeling_router.py`; and `create_lab_request_endpoint` and
  `get_physicians_endpoint` in `src/urolens/domains/request/lab_requests_router.py`. All
  eight now require the canonical session-revocation-aware dependency
  (`app.middleware.rbac.RequireRole` paired with `src.urolens.core.enums.UserRole`) —
  the same pair already used correctly by `patients.py`/`queue.py`/`result_releasing.py`.
  Route logic, request/response shapes, and business behavior are otherwise unchanged;
  the full encryption/service-layer/ID-generation rework for these three files stays
  scoped to the later domain merge (plan doc rows 9–11).
- **Removed the `X-User-Id`-header-as-identity pattern**: `labeling_router.py`'s
  `get_current_user_id` read a client-supplied `X-User-Id` header as the caller's
  identity, falling back to a hardcoded UUID if absent — a forbidden pattern per the
  standards skill. Deleted outright; its one call site (`generate_specimen_label_endpoint`)
  now resolves the operator from the real authenticated user instead.
- **Actor attribution now uses the real authenticated user**: the hardcoded
  `CURRENT_RECEPTIONIST_ID`/`CURRENT_ENCODER_ID` constants and the hardcoded
  `generated_by` UUID in `labeling_router.py`'s offline-label path are gone — each
  patched route now attributes its writes to the user resolved by the new auth
  dependency. This falls directly out of adding real auth (a route that can identify
  its caller has no remaining reason to hardcode one) and wasn't a separate task.
- **Removed the unused `PHYSICIAN_UUID_MAP`** from `lab_requests_router.py` — a dead,
  unreferenced dict hardcoding three UUIDs (one not even valid hex) with a comment
  telling a future dev to replace them.
- **`ENCRYPTION_KEY` now validated at startup**: `app/config.py` previously defaulted
  it to `""`, which only failed the first time `encrypt_pii`/`decrypt_pii` actually ran.
  Now raises `RuntimeError` at import time if unset, same pattern as the JWT fix.
  `src/urolens/core/config.py` was left alone — confirmed (via
  `src/urolens/core/encryption.py`) that encryption always reads `ENCRYPTION_KEY` from
  `app.config`, never from `src/urolens/core/config.py`, so there's nothing to add there.
- **Alembic merge revision `0031`** joins the two previously-orphaned heads (`0011`,
  the notifications branch, and `0030`, the main branch) — pure metadata, no schema
  change. `alembic heads` now reports a single head.
- **Two migration filename/revision-ID mismatches fixed**: renamed
  `0015_analysis_results_add_patient_portal_fields.py` → `0027_...py` and
  `0016_patients_add_user_id.py` → `0028_...py` to match their embedded `revision`
  values (revision content itself untouched).
- **Doc cross-reference corrected**: `docs/backend-consolidation-plan.md` row 3
  pointed to "#7 below" for an urgency comparison that row 7 doesn't support (it's a
  sizing note, not an urgency ranking); repointed to rows 9–10, the actual other
  live-PHI/unauthenticated findings.

## Specimens / labeling / lab-requests domain merge (plan doc rows 9–11)

### Added
- **New service layer**: `src/urolens/services/specimen_service.py` (receive, list,
  reject), `src/urolens/services/labeling_service.py` (search-received, generate
  label, confirm label affixed), `src/urolens/services/lab_request_service.py`
  (create, physician lookup, search pending). `specimens_router.py`,
  `labeling_router.py`, and `lab_requests_router.py` are now thin — every route is
  `Depends(auth) -> await service.fn(db, ...)`, no inline Supabase calls or business
  logic left in any of the three (grep-verified).
- **New SQLAlchemy models**: `LabRequest`, `SampleLabel`, `PrintJob`,
  `SpecimenRejection` (`src/urolens/models/`) — none existed before this merge; all
  four tables were being read/written through raw Supabase REST calls with no ORM
  model at all. `Specimen` gains `medtech_id`, `patient_name`, `patient_uid`,
  `test_type`, `priority_level`, `rejection_reason`, `rejection_note`, `rejected_at` —
  columns already in live use (confirmed via `app/services/specimen_service.py`,
  `seed_specimens.py`, `queue_service.py`) but never modeled.
- **New schemas**: `src/urolens/schemas/specimen.py`, `lab_request.py`,
  `labeling.py` — the three routers previously defined their request/response
  `BaseModel`s inline (standards rule 11 violation); those definitions now live in
  `schemas/` and are imported, matching the pattern `patients.py`/`schemas/patient.py`
  already use.
- **New migration `0032`**: creates the four new tables and adds the eight new
  `specimens` columns above. All statements use `IF NOT EXISTS`/idempotent guards
  (same technique this repo's own pre-Alembic `migration_sql.sql` used) because
  these tables/columns were confirmed live and in active use but had **no prior
  Alembic migration at all** — `alembic/versions/` skips revisions 0007–0009 and
  0012, meaning `specimens`, `lab_requests`, `sample_labels`, `print_jobs`, and
  `specimen_rejections` were all created out-of-band before this migration existed.
  This environment had no network access to the live database to confirm its exact
  current schema before authoring the file, so every statement is written to be a
  safe no-op if the table/column already exists rather than fail — flagged to the
  user before writing it (idempotent-migration approach was the explicitly chosen
  option), and the migration file says the same thing inline. Whoever applies this
  against the real database should diff it against the live schema first.
- **`src/urolens/domains/request/__init__.py`** — was missing; every other domain
  package has one.

### Fixed
- **`specimens.patient_name` now encrypted at rest.** Previously written plaintext
  (no `encrypt_pii` call) despite the labeling router already trying to `decrypt_pii`
  it on read — same encrypted/plaintext hazard pattern the earlier audit found on
  `patients`. `specimen_service.receive_specimen` now encrypts before insert; every
  read path (`list_specimens`, `search_received_specimens`, `generate_label`,
  `confirm_label_affixed`) decrypts. Grep-confirmed no remaining write path stores
  it unencrypted.
- **Decryption failures now surface instead of disappearing.** The original
  `except: name = ""` / `except: name = row.get(...)` silent fallbacks are gone.
  Search results (`labeling_service.search_received_specimens`,
  `specimen_service.list_specimens`) now log a warning and drop the affected row
  rather than return ciphertext or a blank name. Label generation/confirmation
  (`labeling_service._decrypt_patient_name_or_raise`) raises a 422 instead — a
  printed specimen label with the wrong patient name is a patient-safety issue, not
  a display bug, so that path fails loudly rather than printing a guess.
- **`sample_uid`/`request_uid` generation now checks uniqueness before insert**,
  with retry on collision (5 attempts, then a 500) — matching
  `app/services/physician_service.py::_generate_request_uid`, the one place in the
  codebase that already did this correctly. That function used the real current
  date (`REQ-{YYYYMMDD}-NNNNN`); the routers being merged here instead had the year
  hardcoded as the literal string `"2026"` (`SMP-2026-NNNNN`/`REQ-2026-NNNNN`), a
  latent bug that would silently mislabel every ID generated after 2026. Matching
  "whichever pattern is already correct" fixes this as a side effect — both IDs now
  use `{PREFIX}-{YYYYMMDD}-{5 digits}`.
- **`reject_specimen` now has a route-level role gate.** The original
  `app/api/specimens.py` route had none — only an ownership check inside the
  service (standards rule 2 violation: "role check at route/dependency level, never
  ownership-check-only inside a service"). Now gated to `UserRole.MEDTECH` at the
  route (matching who can actually be assigned a specimen, per
  `queue_service.py`), with the ownership check preserved in the service as a
  second layer, not a replacement for the route gate.

### Removed (rule 14 — superseded implementation deleted in the same change)
- `app/api/specimens.py`, `app/services/specimen_service.py`,
  `app/schemas/specimens.py` — deleted. **Task 1 reconciliation decision:**
  `specimen_service.py`'s only route (`reject_specimen`) was real, live,
  functioning logic (mounted, medtech-ownership-checked specimen rejection) — not
  dead code — so it was *ported*, not discarded: its logic now lives in
  `src/urolens/services/specimen_service.reject_specimen`, converted to
  SQLAlchemy, exposed at the same URL (`POST /api/v1/specimens/{specimen_id}/reject`,
  now inside the consolidated `specimens_router.py` instead of a separate mounted
  router). Confirmed zero remaining references to all three deleted files before
  removing them (grep) and confirmed the app still boots with the same total route
  count (52) after the merge — the one route that moved is the only one, nothing
  was silently dropped.
  Track A2's own PHYSICIAN_UUID_MAP removal already covered lab_requests_router.py;
  re-confirmed zero references here — nothing further to remove.
- `src/urolens/domains/request/models.py` — a dead, unreferenced stub (`class
  LabRequest:` with a docstring and a `__tablename__`, no columns, doesn't even
  inherit `Base`). Found while building the real `LabRequest` model above;
  grep-confirmed nothing imported it. `src/urolens/domains/intake/models.py`
  contains a similar dead stub pair (`Patient`, `LabRequest`) but was left alone —
  it's entangled with patient creation (plan doc row 3), out of scope for this
  merge; flagging for whoever does the patients merge to pick up.

## Patients domain merge (plan doc row 3)

### Removed (rule 14 — superseded implementation deleted in the same change)
- **`src/urolens/domains/intake/router.py` deleted in full** (both
  `register_patient_endpoint` and `search_patients_endpoint`, and the router's
  mount in `main.py`). This was a live PHI-exposure bug, not dead code: it wrote
  `first_name`/`last_name`/`date_of_birth` **unencrypted**, with no auth and a
  hardcoded `REAL_USER_ID`, into the same `patients` table
  `src/urolens/services/patient_service.py::PatientService.create_patient`
  writes to encrypted — reachable at a different URL prefix
  (`/api/v1/intake/patients` vs the canonical `/api/v1/patients`), so the table
  could already contain a mix of encrypted and plaintext PII rows. Grep-confirmed
  zero remaining references anywhere in the repo (`domains.intake.router`,
  `register_patient_endpoint`, `REAL_USER_ID`, `PatientRegistrationRequest`) before
  deleting. Route count dropped from 52 to 50, matching exactly the two removed
  endpoints — nothing else was silently dropped.
- **`src/urolens/domains/intake/models.py` deleted** — Task 2 reconciliation: this
  was the dead stub flagged (not yet acted on) during the specimens/labeling merge,
  the twin of the `domains/request/models.py` stub already deleted there. Same
  pattern confirmed here: a `Patient` class and a `LabRequest` class, neither
  inheriting `Base`, no real columns, `__tablename__` only. Grep-confirmed zero
  references anywhere in the repo before deleting — nothing in
  `domains/intake/` (router, service, or elsewhere) ever imported it.

### Fixed
- **`PatientService` moved from Supabase REST to SQLAlchemy** (Task 5) —
  `create_patient`, `search_patients`, and `get_patient_by_user_id` all now use
  `AsyncSession` instead of the Supabase `AsyncClient`. This was not already done:
  despite `PatientService` being the plan doc's designated "correct, keep this"
  implementation, it was 100% Supabase-REST internally before this change. Updated
  both call sites that construct `PatientService`
  (`src/urolens/api/patients.py::get_patient_service`,
  `src/urolens/api/patient_portal.py::get_patient_service`) to inject `AsyncSession`
  via `get_db` instead of the Supabase client via `get_supabase`. Left
  `patient_portal.py`'s *other* dependency, `get_patient_result_service`
  (constructs `PatientResultService`, a different class, results domain, plan rows
  6–8), untouched — out of scope for this merge.
- **`create_patient` now runs as one real transaction.** The prior Supabase-REST
  version had no cross-table transaction, so a failure partway through (e.g.
  consent insert failing after the patient row succeeded) was handled with manual
  compensating deletes of the patient and portal-user rows. With `AsyncSession`,
  the portal user, patient row, and consent record are added and flushed together
  and committed once at the end — an exception before that commit rolls back
  everything via the `get_db` dependency's existing rollback-on-exception handling,
  so the manual compensating-delete code is gone; it's structurally impossible to
  now leave a half-created patient behind.
- **`middle_name` and `clinical_history` added to the `Patient` SQLAlchemy model**,
  plus migration `0033`. Same situation the specimens/labeling merge found:
  `PatientService` was already reading/writing both columns via raw Supabase calls,
  and the dead `domains/intake/models.py` stub even documented `clinical_history`
  as real — but neither was ever modeled in SQLAlchemy. `0033` uses the same
  `ADD COLUMN IF NOT EXISTS` idempotent-migration approach as `0032`, for the same
  reason: this environment has no network access to confirm the live schema before
  authoring the migration.
- **`_generate_patient_uid` is now concurrency-safe** (Task 3). Previously computed
  `max(existing) + 1` with no re-check before insert — two concurrent requests could
  generate and insert the same `patient_uid`. Now reuses the check-then-retry-on-
  collision idiom from the specimens/labeling merge
  (`specimen_service._generate_sample_uid`, `lab_request_service._generate_request_uid`):
  compute a candidate, verify via a direct existence check that it isn't already
  taken, retry (re-scanning for the now-current max) up to 5 attempts, then fail
  with a 500. The candidate itself is still the sequential `PAT-NNNNNN` format, not
  switched to the specimens/lab-requests random-suffix format — the pattern being
  reused is the safety idiom, not the ID shape, which this task didn't ask to change.

### Verified (Task 4 — re-checked, not assumed)
- `src/urolens/api/patients.py` already used the canonical
  `app.middleware.rbac.RequireRole` paired with `src.urolens.core.enums.UserRole`
  on both routes — confirmed by reading the file, not assumed from the plan doc.
  No change needed here.

## Image / AI analysis domain merge (plan doc rows 4–5)

### Fixed
- **Plan doc row 4's filenames were swapped relative to their actual content —
  found and corrected before building anything.** Direct inspection showed
  `ai_integration.py` (not `ai_integration_service.py`, as the doc said) was the
  one with the stale schema (`ImageStatus.UPLOADED`/`ResultStatus.PENDING_REVIEW`,
  neither a real enum member on the current models) and the broken
  `from ..core.config import settings` import. `ai_integration_service.py` was
  the schema-correct one, with a working import
  (`from ..core.config import AI_MODEL_VERSION, S3_BUCKET`, both real flat
  constants). Built the canonical service by rewriting `ai_integration_service.py`
  in place — the file the plan doc described as "not fixable, not worth
  salvaging" is actually the one that survived. Added a correction note to the
  plan doc's row 4 rather than silently deviating from it.
- **Canonical `AIIntegrationService` built** (`src/urolens/services/ai_integration_service.py`,
  rewritten in place). Owns the full upload → validate → store → infer →
  Smart-Diagnosis-precompute pipeline. Ported in, unchanged in behavior, all of
  the production-hardening that previously only existed inline in the router:
  dash→underscore particle-class normalization, `asyncio.to_thread` wrapping
  for both the blocking `PIL.Image.open` resolution check and the blocking
  YOLOv8 `infer()` call, and the smart-diagnosis-at-upload precompute (writes
  `analysis_results.smart_diagnosis` only — `smart_diagnosis_outputs` stays
  `SmartDiagnosisService`'s job at confirmation time). Storage upload failure
  stays non-fatal (logged, not raised) — same production behavior the router
  already had; not revisited as part of this merge.
- **Storage switched from S3/boto3 to Supabase Storage** — the service's
  `_upload_to_storage` now calls `sb.storage.from_(SUPABASE_IMAGE_BUCKET).upload(...)`
  (the same client/bucket the router was already using) instead of
  `boto3.client("s3").upload_fileobj(...)`. Matches the plan's global
  architecture decision: no AWS credentials exist anywhere in this project.
- **`_get_or_create_result`'s patient lookup moved off raw Supabase onto the
  `LabRequest` SQLAlchemy model** — that model didn't exist when this method was
  originally written; it does now (built in the specimens/lab-requests merge).
  The `try/except: pass` around the old Supabase call is also gone — a
  SQLAlchemy `scalar_one_or_none()` returning `None` for "not found" doesn't
  need one.
- **Incidental bug fix**: the router's response always hardcoded
  `flagged_anomalies=None` regardless of what inference actually found. Now
  that the router builds its response from the real `AnalysisResult` object
  the service returns, `flagged_anomalies` reflects the actual computed value.
- **`image.py` de-inlined** (Task 2) — `upload_image` and `discard_image` are
  now `Depends(auth) → await service.method(...)`, no inline Supabase/storage
  calls left in the router file (grep-verified). Its two inline `BaseModel`s
  (`AnalysisResultResponse`, `ImageDiscardResponse`) moved to
  `src/urolens/schemas/image.py`, matching the pattern established in the
  specimens and patients merges.
- **RBAC/`UserRole` swapped to the canonical pair** (Task 4) — `image.py` now
  imports `RequireRole` from `app.middleware.rbac` and `UserRole` from
  `src.urolens.core.enums`, not `src.urolens.middleware.rbac`/`models.user`.
  Confirmed `UserRole.MEDTECH` is still the right role for both routes — image
  upload/analysis and discard/retake are MedTech actions, matching the role
  gating the specimens merge used for labeling. This was a real, breaking
  behavior change for the test suite, not a no-op: the canonical dependency
  checks session revocation via `is_session_active()`, which hits Supabase — the
  non-canonical one these tests were written against only decoded the JWT
  locally. Fixed by adding an autouse `mock_session_active` fixture to
  `tests/integration/conftest.py` (patches
  `app.middleware.rbac.is_session_active`) rather than leaving 6 previously-passing
  tests broken — see Test suite section below.

### Removed (rule 14 — superseded implementation deleted in the same change)
- **`ai_integration.py` deleted** (Task 1) — stale schema and a broken import
  (see the plan-doc correction above); zero references anywhere, confirmed by
  grep before deleting.
- **`app/api/images.py`, `app/schemas/images.py`, `app/services/image_service.py`
  deleted** (Task 3) — re-confirmed zero references before deleting, not taken
  on faith from the original audit. `src/urolens/api/image.py` + `ImageRetakeService`
  remain the sole discard/retake implementation, unchanged internally (still
  Supabase-REST — converting it wasn't in this merge's task list, noted as a
  residual item below).
- **`boto3`, `botocore`, `s3transfer` removed from `requirements.txt`** (Task 5)
  — confirmed zero code references first (the only remaining hits are a
  docstring line in the new service explaining *why* it isn't using them).
  Removed via a byte-safe script rather than a normal text edit:
  `requirements.txt` has mixed encoding (UTF-16 for most of the file, plain
  UTF-8/ASCII for the tail where these three lines lived) left over from
  however it was last edited — not fixed as part of this change, out of scope.

### Schema-drift check (new process from this merge onward)
- **Stop-condition was checked and NOT triggered.** Compared every column the
  upload/inference/discard code paths read or write against the current
  `Image` and `AnalysisResult` SQLAlchemy models — all of them
  (`storage_key`, `file_format`, `width_px`, `height_px`, `file_size_bytes`,
  `status`, `ai_findings`, `flagged_anomalies`, `particle_classes`,
  `model_version`, `smart_diagnosis`, `patient_id`, etc.) are already modeled.
  No third unverified migration was needed or added.

### Test suite
- **6 previously-passing tests in `tests/integration/test_image_upload_and_inference.py`
  broke as a direct, expected consequence of the RBAC swap above** (canonical
  auth's session-revocation check hits Supabase; the test fixtures never
  needed to mock that before) — fixed, not left broken:
  `test_upload_unsupported_format_returns_422`,
  `test_upload_below_minimum_resolution_returns_422`, and all four discard
  tests. Fix: added the autouse `mock_session_active` fixture described above,
  plus corrected each test's stale `patch("src.urolens.api.image.sb", ...)`
  target — that attribute no longer exists on the now-de-inlined router. Discard
  tests dropped the patch entirely (nothing in the discard path touches it);
  upload tests point it at `src.urolens.services.ai_integration_service.sb`,
  where the Supabase Storage call now actually lives.
- **3 tests remain failing, unchanged from the pre-existing baseline**:
  `test_upload_valid_image_returns_201`,
  `test_upload_triggers_ai_inference_when_package_available`,
  `test_upload_succeeds_when_ai_inference_fails`. These need a full successful
  upload, which now runs real SQLAlchemy queries (`Specimen`/`LabRequest`
  lookups, `Image`/`AnalysisResult` insert) that this test file's mocking
  strategy — an `AsyncMock` standing in for the Supabase client — was never
  built to support; a real or properly-mocked `AsyncSession` would be needed to
  fix these, which is a materially larger, separate piece of test-infrastructure
  work. Not attempted here since these three were already failing before this
  merge (confirmed by running the suite pre- and post-change) — not a
  regression this merge introduced, so leaving them failing doesn't violate
  "same or fewer failures than baseline."
- Net result: 20 failed / 28 passed — identical to the baseline, both in count
  and in which specific tests fail.

### Residual items not addressed in this merge (out of scope, noted for later)
- `ImageRetakeService` (discard/retake) is still 100% Supabase REST — the plan's
  "SQLAlchemy AsyncSession as the primary pattern" hasn't reached this class
  yet. Not part of this merge's task list; flagging per the plan's own phased
  rollout note ("not done in one big-bang pass").
- The 3 upload-success-path tests above need real or mocked SQLAlchemy session
  support to ever pass — flagging as future test-infrastructure work, not
  attempted here.
- `requirements.txt`'s mixed UTF-16/UTF-8 encoding — touched only the minimum
  necessary to remove the three dependency lines; not normalized to a single
  encoding.

## Results merge — Step 5a: confirm/override (plan doc row 6 only)

Row 7 (supervisor review/approval) and row 8 (result releasing/notifications) are
untouched — separate follow-up tasks, not started here.

### Fixed
- **Two plan-doc row 6 corrections found and fixed before acting on them, same
  as the image/AI merge's row-4 filename swap:**
  1. `app/services/result_service.py` could **not** be deleted "outright" as the
     row said. It also contains `get_smart_diagnosis`, used by
     `app/api/results.py`'s `GET /{result_id}/smart-diagnosis` route — a route
     that belongs to neither row 6 (doesn't confirm or override anything) nor
     row 7 (not supervisor-gated business logic, just a result-detail read) and
     was left untouched. Deleting the whole file would have broken a live,
     unrelated route. Only `confirm_result` and `override_parameter` were
     removed from it (along with the `datetime`/`_PHT` imports only they used).
  2. `src/urolens/api/results.py` needed far more than a RBAC-pair swap before
     it could be mounted. It already contained full row-7-shaped routes
     (`/pending`, `/{id}/full`, `/annotate`, `/approve`, `/return`, `/escalate`)
     bridged directly to the old, unported `app.services.result_review_service`,
     plus two generic result-viewing routes (`GET /{id}`, `GET /{id}/smart-diagnosis`)
     backed by inline Supabase calls. Both files share the `/api/v1/results`
     prefix; mounting `src/urolens/api/results.py` as-is alongside the
     still-live `app/api/results.py` (deliberately untouched, row 7's job) would
     have silently shadowed one router's routes with the other's, in
     registration order, at every one of these paths, with FastAPI raising no
     error: `/pending` (exact duplicate), `/{id}/full` vs `/{result_id}` (both
     single-segment-param GETs — `/{id}` from the new file would have matched
     first and swallowed `/{result_id}`'s traffic since it registers earlier),
     `/{result_id}/annotate`, `/approve`, `/return`, `/escalate` (exact
     duplicates), and `/{id}/smart-diagnosis` vs `/{result_id}/smart-diagnosis`
     (same collision pattern as `/{id}` vs `/{result_id}`). Resolved by removing
     all of the above from `src/urolens/api/results.py` before mounting it —
     the file now contains only `confirm_result`/`override_parameter`, their
     supporting schemas, and their dependency factories. The removed route
     definitions are recoverable from git history; they were already bridged
     to the *old* service, so nothing of row 7's actual porting work was lost.
  Both corrections added to `docs/backend-consolidation-plan.md` row 6.
- **`src/urolens/api/results.py` mounted** in `main.py` as
  `results_confirm_override_router`, alongside `app/api/results.py`'s
  `results_router` (kept, distinct variable name — both are needed
  simultaneously; row 7's routes aren't ported yet). Confirmed zero path
  collision by listing every route on both routers post-change, not just by
  inspecting the two files' path strings separately.
- **RBAC/`UserRole` swapped to the canonical pair** — `src/urolens/api/results.py`
  now imports `RequireRole` from `app.middleware.rbac` and `UserRole` from
  `src.urolens.core.enums`, not `src.urolens.middleware.rbac`/`models.user`.
  `MEDTECH` (confirm) and `MEDTECH`/`SUPERVISOR` (override) role gates
  unchanged — already correct for what these routes do.
- **`ManualOverrideService`'s re-derivation traced end-to-end, not just
  confirmed present in source**: `override_parameter` accepts
  `original_ai_value` as a parameter (kept for API-contract compatibility —
  the request body still requires it) but never uses it; `_extract_original_value`
  reads the AI-generated value for that parameter from the stored
  `AnalysisResult.ai_findings` instead, and that's the value actually persisted
  to `ManualOverride.original_ai_value`. Confirmed by reading the full call
  path, not by re-stating the plan doc's claim.

### Removed (rule 14 — superseded implementation deleted in the same change)
- **`confirm_result`/`override_parameter` removed from `app/services/result_service.py`
  and `app/api/results.py`** — the weaker, client-trusting versions. Left
  `app/schemas/results.py` untouched even though `ConfirmResultRequest`,
  `OverrideParameterRequest`, and `OverrideParameterResponse` are now unused
  there — that file is shared with row 7/8's schemas and wasn't in this task's
  scope; noted here rather than reached into.

### Test suite
- No change from the image/AI merge's baseline: 20 failed / 28 passed, identical
  in count and in which specific tests fail. Confirm/override has no existing
  test coverage to begin with (no `test_results.py` or similar in `tests/`) —
  nothing to break, nothing fixed.

### Verified
- `app/api/results.py`'s remaining routes (`get_smart_diagnosis`, `get_supervisor_stats`,
  `list_approved_today`, `list_escalated`, `list_pending_results`, `get_full_result`,
  `annotate_result`, `approve_result`, `return_result`, `escalate_result`) diffed
  against the pre-change file — confirmed the only removals are the two
  confirm/override routes and their now-unused imports; nothing in the
  review/approval logic touched.
- App boots clean: 50 routes before and after (2 removed from `app/api/results.py`,
  2 added via `src/urolens/api/results.py` — net zero, confirmed by listing
  every `/results` route post-change). OpenAPI generates cleanly (45 paths,
  unchanged).

## Results merge — Step 5b: supervisor review/approval port + releasing/notifications (plan rows 7–8)

### Fixed
- **`notifications.py` swapped to the canonical RBAC/`UserRole` pair** (row 8) —
  confirmed accurate before acting (5a found row 6 wrong twice, so this was
  re-verified rather than trusted): it was importing
  `RequireRole`/`get_current_user` from `src.urolens.middleware.rbac`
  (no session-revocation check) and `UserRole` from `src.urolens.models.user`.
  Now imports both from the canonical pair. Import-only change; role gating
  unchanged. Verified the rest of row 8's claim too —
  `src/urolens/api/result_releasing.py` already uses the canonical pair and
  needed nothing further.
- **Plan doc row 7 correction**: its method list (`get_pending`,
  `get_approved_today`, `get_escalated`, `save_annotation`, `approve_result`,
  `return_result`, `escalate_result`) was incomplete — the live
  `app/services/result_review_service.py` also has `get_supervisor_stats` and
  `get_full_result` (backing live routes `GET /supervisor/stats` and
  `GET /{result_id}`), 9 methods total. Both ported along with the other 7.
- **Plan doc row 7 schema-drift claim was also wrong in the opposite
  direction from rows 6's corrections** — this time the doc's caution turned
  out to be more pessimistic than reality. All four tables
  `result_review_service.py` touches (`result_reviews`, `result_approvals`,
  `result_returns`, `escalations`) already have complete Alembic history —
  migration `0019` ("create result_reviews, result_approvals, result_returns,
  escalations tables") — despite having no SQLAlchemy models. This is *not*
  the migrations-`0032`/`0033` schema-drift pattern (DDL missing entirely);
  it's just "nobody wrote the ORM mapping for an already-migrated table," which
  is normal port work. Models added for all four, no new migration needed.
- **Two real, narrower schema-drift findings did surface**, and — per this
  consolidation's standing rule (introduced at the image/AI merge) — neither
  was fixed with a migration:
  - `analysis_results.confirmation_notes`: zero Alembic history anywhere.
    Already effectively dead going forward (step 5a's canonical confirm path
    never accepted or wrote a `notes` parameter), but old rows may have real
    historical values. The ported `get_full_result` always returns `None` for
    this field now — a real, minor information-loss regression for
    *historical* data specifically, reported here rather than silently
    absorbed.
  - `result_reviews.spatial_annotations`: zero Alembic history anywhere, and
    unlike `confirmation_notes` this is live functionality —
    `annotate_result` writes it today. Not modeled and not persisted in the
    port (the real column type — JSON? JSONB? array? — isn't knowable without
    checking the live database, which this sandbox cannot reach, and guessing
    wrong risked a subtly broken raw-SQL write). `save_annotation` still
    accepts and returns `spatial_annotations` in its request/response shape
    for API-contract compatibility, but a caller-supplied value is silently
    not persisted. **This is a real functionality gap, not a cosmetic one —
    flagging prominently for whoever verifies migrations `0032`/`0033`/this
    finding together**, since spatial annotation coordinates are presumably
    used for something in the supervisor review UI.

### Added
- **New SQLAlchemy models**: `ResultReview`, `ResultApproval`, `ResultReturn`,
  `Escalation` (`src/urolens/models/`) — all four tables already had Alembic
  history (migration `0019`) but no ORM mapping before this. `Escalation.escalation_path`
  is modeled as a plain string even though the live column is a native
  Postgres enum type (`CREATE TYPE escalation_path AS ENUM (...)`), matching
  this codebase's established convention for status-like columns elsewhere
  (`Specimen.status`, `AnalysisResult.status` — none use a SQLAlchemy `Enum`
  type); validity is checked in the service layer, same as the pre-port code.
- **New service**: `src/urolens/services/result_review_service.py` — a real
  SQLAlchemy rewrite of all 9 methods from `app/services/result_review_service.py`,
  not a wrapper around the old Supabase calls. Every read path that touches
  `Specimen.patient_name` or `Patient.first_name`/`last_name`/`date_of_birth`
  now decrypts it (`_decrypt_or_none`, matching the skip-on-failure pattern
  already established in `specimen_service.py`/`labeling_service.py`) — the
  original service predates the specimens merge's encryption fix and just
  returned these fields raw. `approve_result` still marks the specimen
  `COMPLETED` as a side effect (using the `status`/`completed_at` columns the
  specimens merge already added to the `Specimen` model).
- **New schemas**: `src/urolens/schemas/result_review.py`, mirroring the
  response shapes already live via `app/schemas/results.py` (not reusing that
  module directly — keeps this file's dependency on the `app/` tree limited
  to what step 5a already established for confirm/override).
- **9 supervisor routes added** to `src/urolens/api/results.py`:
  `GET /supervisor/stats`, `/approved-today`, `/escalated`, `/pending`,
  `PATCH /{result_id}/annotate`, `POST /{result_id}/approve`, `/return`,
  `/escalate`, `GET /{result_id}`. Canonical auth pair throughout,
  `RequireRole([UserRole.SUPERVISOR])` — confirmed against
  `app/api/results.py`'s actual current routes (all nine were already
  SUPERVISOR-only there), not guessed. Registration order preserved
  deliberately: literal-path GETs before the catch-all `GET /{result_id}`,
  matching the ordering the pre-port file already relied on to avoid the
  catch-all shadowing them.
- **Baseline Tier-1 test coverage** — `tests/test_manual_override_service.py`
  (3 tests: the client-supplied-`original_ai_value`-is-ignored behavior 5a
  only traced by hand, plus the unknown-parameter and
  already-finalised-result rejections) and `tests/test_result_review_service.py`
  (5 tests: one happy path each for approve/return/escalate, the shared
  `_require_pending` guard, and `escalate_result`'s path validation). Not
  exhaustive — `ResultReviewService`'s five read-side methods and
  `save_annotation` remain untested, flagged as a gap rather than silently
  left uncovered. Full suite: 20 failed / 36 passed — same 20 pre-existing
  failures, unchanged; 8 new tests, all passing.

### Removed (rule 14 — superseded implementation deleted in the same change)
- **`app/services/result_review_service.py` deleted outright** — fully ported,
  zero remaining references confirmed by grep (excluding docstring/comment
  mentions of its history) before deleting.
- **7 routes removed from `app/api/results.py`** (the ones just re-added to
  `src/urolens/api/results.py`, backed by the new service) — leaving only
  `GET /{result_id}/smart-diagnosis`, which belongs to neither row 6 nor row 7
  (it's not confirm/override, and it isn't supervisor-review business logic —
  just a result-detail read gated to SUPERVISOR) and stays there, flagged
  rather than moved or touched, per this task's explicit instruction.
- **`app/schemas/results.py` pruned** from 208 lines to ~30 — everything except
  `SmartDiagnosisResponse` and its two variants was only used by the routes
  just removed. `ConfirmResultRequest`/`OverrideParameterRequest`/
  `OverrideParameterResponse` (already unused since step 5a, left alone at the
  time since most of the file was still in use) are gone too now that nothing
  in the file is needed beyond the smart-diagnosis shapes.
