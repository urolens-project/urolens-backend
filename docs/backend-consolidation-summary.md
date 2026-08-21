# Backend Consolidation — Final Summary

Status: **all domains from `docs/backend-consolidation-plan.md` merged.** This document
closes out the consolidation — see that file for the original audit and the per-domain
correction notes; see `CHANGELOG.md` for the full, chronological, file-level detail behind
every point below.

## What this was

`urolens-backend` had grown two independently-evolved trees reading/writing the same data —
a legacy `app/` tree (Supabase REST, its own RBAC) wired to "mobile" routers, and a newer
`src/urolens/` tree (SQLAlchemy models, a second RBAC) wired to "web" routers. The goal: one
API, one router tree organized by domain, one service layer per domain, one auth/RBAC
dependency, one DB-access pattern, one config surface.

## Merge order and what happened in each domain

1. **Specimens / labeling / lab-requests** — highest-severity domain by auth-gap count, not
   just first by convention: `labeling_router.py` authenticated via a client-supplied
   `X-User-Id` header (the standards skill's own named forbidden pattern), two other routes
   had no auth at all. Full SQLAlchemy service layer built; found and formalized (idempotent
   migration `0032`) four tables/columns that were live and in use but had no prior Alembic
   history at all (`lab_requests`, `sample_labels`, `print_jobs`, `specimen_rejections`,
   several `specimens` columns) — created out-of-band before this migration existed.
2. **Patients** — `src/urolens/domains/intake/router.py` wrote patient PII **unencrypted**,
   no auth, hardcoded actor ID, into the same table `PatientService` wrote to encrypted —
   deleted. `PatientService` itself was still 100% Supabase REST despite being the "correct,
   keep this" implementation — converted to SQLAlchemy, with `create_patient` gaining a real
   single-transaction commit (replacing manual compensating-delete logic Supabase REST's lack
   of cross-table transactions had required). Second schema-drift finding, same pattern:
   migration `0033` (`patients.middle_name`, `.clinical_history`).
3. **Image / AI analysis** — three implementations existed, none production-ready. Found the
   plan doc had the two AI-integration files' properties swapped relative to their actual
   filenames (the one named as "stale" was schema-correct; the one named as "correct" had the
   stale schema and a broken import) — corrected before building anything. Canonical service
   rebuilt with the router's proven production-hardening ported in; storage moved from
   S3/boto3 (never actually wired to real infrastructure — no AWS credentials exist anywhere
   in this project) to Supabase Storage, the one actually in use.
4. **Results: confirm/override** — kept `ManualOverrideService`'s re-derivation of
   `original_ai_value` from stored `ai_findings` over the weaker implementation that trusted
   a client-supplied value. Mounting the "keep" file required first stripping row-7-shaped
   routes out of it — it already had them, bridged to the old, unported `result_review_service`,
   and would have silently path-collided with the still-live legacy file.
5. **Results: supervisor review/approval** — the single largest piece of this consolidation
   (636-line service, no prior SQLAlchemy counterpart). Real rewrite, not a wrapper: all 9
   methods (plan doc's list of 7 was itself incomplete — `get_supervisor_stats` and
   `get_full_result` were missing) ported against new SQLAlchemy models. **Found and fixed a
   real regression** introduced by the port itself: `spatial_annotations` was accepted by
   `annotate_result` but never actually persisted (SQLAlchemy only persists mapped
   attributes; the pre-port Supabase-REST code needed no such mapping) — silently dropped
   every supervisor's spatial annotation data until caught and fixed (migration `0034`).
6. **Results releasing / notifications** — smallest step; `result_releasing.py` was already
   correct, `notifications.py` needed its auth-import swap.
7. **Auth/RBAC + config (this task)** — full-repo sweep confirmed every domain's swap held,
   with one real straggler (`notification_service.py`) fixed. Deleted the non-canonical
   `src/urolens/middleware/rbac.py` and the duplicate `UserRole` enum. Consolidated
   `app/config.py` and `src/urolens/core/config.py`'s flat `os.getenv` constants into one
   `Settings` class, every secret validated at startup. Deleted the three empty,
   unreferenced stub domain packages (`diagnostics`, `distribution`, `tracking`).

## Key decisions

- **SQLAlchemy `AsyncSession` is the primary DB-access pattern** for all domain services
  going forward. Reasoning (from the original plan): the highest-stakes workflow in this
  codebase, result confirmation, already depended on SQLAlchemy's transaction/SAVEPOINT
  semantics for correctness — a property Supabase REST doesn't have. Applied domain by domain
  as each was merged, not as one big-bang pass. **Not fully complete** — see Outstanding work.
- **Supabase Storage over S3/boto3** for images — confirmed via `.env.example`/
  `requirements.txt` that no AWS credentials exist anywhere in this project; the boto3 code
  path was never wired to real infrastructure. `boto3`/`botocore`/`s3transfer` removed from
  `requirements.txt` once nothing referenced them.
- **`src/urolens/core/enums.py`'s `UserRole` kept** over the duplicate in
  `src/urolens/models/user.py` — it's what every already-correct router already imported;
  the duplicate is now deleted entirely (this task).
- **Canonical auth dependency stays at `app/middleware/rbac.py`** — not relocated to
  `src/urolens/core/rbac.py`, though the plan doc allowed either. Reasoning: every router
  across *both* trees already imports successfully from this one location (confirmed by a
  full-repo grep, zero stragglers after one fix). `app/`'s own routers
  (`auth.py`/`physician.py`/`sync.py`) — never part of any duplicate pair, explicitly out of
  scope for every task in this consolidation — also import from here. Relocating would have
  required editing those three protected files for zero functional benefit, since the module
  already serves as the single canonical source regardless of which directory it lives in.
- **Config: one `Settings` class in `src/urolens/core/config.py`**, built on plain
  `pydantic.BaseModel` rather than the `pydantic-settings` package the task asked for —
  `pydantic-settings` isn't installed and this sandbox has no network access to add it;
  shipping code that imports a missing package would fail the "app boots clean" verification
  that every step of this consolidation has required. Documented in the module's own
  docstring as a deliberate, low-risk-to-later-fix deviation, not a silent substitution.
  Every secret (`JWT_SIGNING_KEY`, `ENCRYPTION_KEY`) raises `RuntimeError` at startup if unset
  or equal to a known placeholder — verified this actually works, not just read the code (the
  real `.env` was temporarily moved aside, each failure case triggered and confirmed, then
  `.env` restored and diff-verified byte-identical to a backup taken first).

## Deleted across the whole consolidation

`src/urolens/domains/intake/router.py` (unencrypted duplicate patient creation) ·
`src/urolens/domains/intake/models.py`, `.../request/models.py` (dead stubs) ·
`app/api/specimens.py`, `app/services/specimen_service.py` (logic ported, not discarded — see
row 9's reconciliation in the plan doc), `app/schemas/specimens.py` ·
`app/api/images.py`, `app/schemas/images.py`, `app/services/image_service.py` ·
`src/urolens/services/ai_integration.py` (stale schema, broken import) ·
`app/services/result_service.py`'s `confirm_result`/`override_parameter` (kept
`get_smart_diagnosis` — still used by a route outside rows 6–7) ·
`app/services/result_review_service.py` (fully ported) ·
`src/urolens/middleware/rbac.py` + its `__init__.py` ·
`src/urolens/models/user.py`'s duplicate `UserRole` ·
`app/config.py` ·
`src/urolens/domains/{diagnostics,distribution,tracking}/` (empty stub packages).

## Migrations added (all pending live-schema verification — see below)

`0031` (merges the two pre-existing orphaned Alembic heads) · `0032` (specimens/lab-requests/
labeling schema formalization) · `0033` (`patients.middle_name`/`.clinical_history`) · `0034`
(`result_reviews.spatial_annotations`, fixing the persistence regression above). `0032`–`0034`
all use the same idempotent `ADD COLUMN IF NOT EXISTS`/`CREATE TABLE IF NOT EXISTS` guard
idiom, for the same reason: this sandbox never had network access to confirm the live
database's actual current schema, so every one of them is written to be a safe no-op against
a database that already has the column/table, rather than assumed-safe from a guess.

## Outstanding work — deliberately deferred, not resolved here

- **Migrations `0032`, `0033`, `0034` all need a live-schema verification pass** before
  anyone runs `alembic upgrade` against a real environment — diff each one against the
  database's actual current state. This is the single most important follow-up from the
  entire consolidation.
- **`analysis_results.confirmation_notes`** — same missing-Alembic-history pattern as the
  columns above, found during the results/supervisor-review merge. Deliberately *not* given
  the same urgency as `spatial_annotations` (which was actively regressing live
  functionality) — this one is historical-data-loss-only (the field is already dead going
  forward; the canonical confirm path never writes it). Add to the same verification batch as
  `0032`–`0034` rather than guess at its type separately.
- **`ImageRetakeService`** (discard/retake, image/AI domain) is still 100% Supabase REST
  internally — flagged during that merge, not required by that task's scope. The SQLAlchemy
  migration mentioned above is not yet complete for this one class.
- **`ResultReleasingService`** (`src/urolens/api/result_releasing.py`) is also still
  Supabase-REST internally. Row 8 of the plan only required `notifications.py`'s auth-import
  swap — the service's own DB-access pattern was never in scope to change.
- **`PatientResultService`** (`patient_portal.py`'s other dependency, alongside
  `PatientService`) was never touched by any step of this consolidation — still Supabase-REST.
- **Config built on plain `pydantic.BaseModel`, not `pydantic-settings`** — a deliberate,
  documented deviation (see Key decisions above). Follow-up: once `pydantic-settings` can
  actually be installed (network access, or vendored), swap `Settings`' base class and remove
  `_load_settings()`'s manual `os.getenv()` calls in favor of automatic env-var binding — the
  class shape doesn't need to change for that swap.
- **Test coverage is baseline, not exhaustive**, across the whole consolidation. Notably:
  `ResultReviewService`'s read-side methods (`get_pending`, `get_approved_today`,
  `get_escalated`, `get_supervisor_stats`) and `save_annotation`'s `annotation_notes` path
  remain untested (only the `spatial_annotations` regression fix has direct coverage);
  `PatientService`, `specimen_service.py`, `labeling_service.py`, and `lab_request_service.py`
  have no dedicated unit tests at all — the consolidation's testing effort focused on the
  Tier-1 confirm→override→approve/return/escalate chain per the standards skill, not the
  domain services around it.
- **ID-generation for specimens/lab-requests and patients is O(n) per creation** — scans all
  existing UIDs to compute the next one, with a check-then-retry loop added for concurrency
  safety but not for performance. Noted during the specimens and patients merges as a known,
  accepted limitation, not addressed.
- **`requirements.txt` has mixed UTF-16/UTF-8 encoding**, left over from however it was last
  hand-edited before this consolidation. Touched only the minimum necessary (removing the
  boto3 lines during the image/AI merge) — never normalized to one encoding.
- **`app/api/auth.py`, `app/api/physician.py`, `app/api/sync.py`** were never part of any
  duplicate pair in the original plan and were explicitly out of scope for every task in this
  consolidation — they remain exactly as they were, still Supabase-REST, still part of the
  legacy `app/` tree. Not a gap, a deliberate boundary: nothing in the audit ever found a
  newer counterpart to consolidate them against.

## A note on process

Every domain merge in this consolidation found the plan doc's stated facts didn't fully match
current code at least once — a filename/property swap (image/AI), two separate corrections in
one row (results confirm/override: the file to delete couldn't be deleted outright, and the
file to keep needed far more than an import swap), an incomplete method list plus a
schema-drift claim that ran the opposite direction from expected (results supervisor-review).
Each was found by reading the actual current code before acting, not by trusting the audit
document — and each was corrected in the plan doc itself, with the correction left visible
alongside the original claim, rather than silently rewritten. If a future reader finds another
mismatch, the same process applies: verify against real code, fix and note the correction,
don't silently follow a stale instruction either.
