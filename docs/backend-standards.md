# Backend standards and rules

This is the full rule reference for `urolens-backend`. `CONTRIBUTING.md` has the short, actionable version (the required sequence for adding a feature and the pre-PR checklist); this document explains *why* each rule exists and, where it matters, the actual current state of the codebase against it — several of these changed materially during recent consolidation work, and a stale restatement would mislead more than it helps.

Rules 1-5 are security/PHI-critical — check these on every change, no exceptions. Rules 6-16 are structure and code-quality.

## 1. Config

All configuration goes through `src/urolens/core/config.py`'s `Settings` class — no `os.getenv` calls anywhere else in the codebase. Secrets (`JWT_SIGNING_KEY`, DB credentials, `ENCRYPTION_KEY`) have no default; the app raises `RuntimeError` at import time if any of them is unset or still equal to a known placeholder value. This is deliberate and load-bearing — see `README.md`'s setup section for what this looks like when you hit it.

## 2. Auth / RBAC

Every route gets the canonical session-revocation-aware dependency: `Depends(RequireRole([...]))` or `Depends(get_current_user)`, both from `src/urolens/core/rbac.py`. Role checks happen at the route/dependency level — never as an ownership-check-only inside a service. No client-supplied identity header (`X-User-Id` or similar) is ever trusted as authentication. (This exact anti-pattern existed in this codebase before the backend consolidation and was removed — see `changelog.md`'s "Specimens / labeling / lab-requests domain merge" entry.)

## 3. PHI / PII

Encrypted at rest via Fernet (`src/urolens/core/encryption.py`), one policy, no second unencrypted path for the same entity. Never log or print decrypted PHI, key material (even partial), or raw passwords. PHI-bearing files/images are served only through an authenticated + audited endpoint, never a public storage URL.

## 4. CORS

Never combine `allow_origins=["*"]` with `allow_credentials=True`. `main.py` uses `allow_origins=["*"]` with `allow_credentials=False` deliberately — this API is Bearer-token-in-header only, never cookie-based, so credentialed CORS is never needed. Don't flip `allow_credentials` to `True` without re-examining this.

## 5. IDs and attribution

Real-world record IDs (specimen sample UIDs, lab request UIDs, patient UIDs) get a pre-insert uniqueness check with retry-on-collision — see `_generate_request_uid`/`_generate_sample_uid`/`_generate_patient_uid` in the relevant services for the established pattern. No hardcoded actor/user UUID in source, ever — if a route can't identify the real authenticated caller, it can't write data attributed to someone.

## 6. Package boundaries

**Current state**: `src/urolens/core/`, `services/`, `schemas/`, and `models/` all have populated `__init__.py` files re-exporting their public API (e.g. `from src.urolens.services import PatientService`). This was added deliberately additive-only — most *existing* call sites in this codebase still import submodules directly (`from src.urolens.services.patient_service import PatientService`), and migrating them wasn't done as part of that change. New code should prefer importing from the barrel where practical; don't feel obligated to rewrite working imports you're not otherwise touching.

`src/urolens/api/` and `src/urolens/domains/` deliberately do **not** have barrels — every one of their 14 router modules exports a symbol literally named `router`, an unavoidable collision a flat re-export can't resolve. `main.py` handles this today by aliasing each router import individually; import those submodules directly.

`schemas/__init__.py` excludes two pairs of classes that share a name across sibling modules but are genuinely different types: `LabRequestCreateRequest` (`schemas/lab_request.py` vs `schemas/physician.py`) and `ApprovedResultItem` (`schemas/result_review.py` vs `schemas/result_releasing.py`). Both are only reachable via direct submodule import — check there first if you get an unexpected class when importing one of these two names from the barrel.

## 7. Imports

The actual convention in this codebase: **absolute** `from src.urolens.<package>.<module> import X` in top-level files (`main.py`, `tests/`) and **relative** `from ..<package>.<module> import X` / `from .<module> import X` within `src/urolens/` package modules themselves. Don't climb more than one level of relative import across a domain boundary — if you need to reach far, use the absolute form instead.

## 8. Response envelope

One shared success/error model for every route — no raw dicts from handlers. `main.py`'s global exception handlers wrap every `HTTPException`/`RequestValidationError` into `{"error": {"code": "...", "message": "..."}}` so the API is consistent regardless of which route raised.

## 9. Docstrings

Every exported (non-underscore) module-level function/class/constant gets a docstring: one-line summary, `Args:` for each parameter whose purpose isn't obvious from its type hint, `Returns:` for any non-`None` return, `Raises:` for exceptions the caller must handle, `Example:` for non-trivial logic. Google-style formatting (`pyproject.toml`'s `[tool.ruff.lint.pydocstyle] convention = "google"`). Private helpers get a short comment if their purpose isn't obvious from the name.

**Current state**: a full docstring pass ran across `src/urolens/`'s entire exported surface — confirmed via Ruff's `D`-category report (`docs/ruff-baseline-report.md`): zero missing-docstring violations (`D100`-`D105`) remain in `src/urolens/`, only Google-convention formatting nitpicks (blank-line placement, a handful of undocumented `__init__`s). `tests/` and the root-level scripts (`seed_*.py`, `test_image_upload.py`) were never in that pass's scope and don't carry the same expectation — docstrings on `test_*` functions aren't a convention this codebase follows.

## 10. Typing

Every exported function/method should declare explicit parameter and return types — no relying on inference for anything another module consumes. No `Any` — use unions + `isinstance` narrowing, or a Pydantic model/`TypedDict` at boundaries.

**Current state**: Ruff's `ANN` category is enabled and reporting (`ruff check .`), not yet enforced or build-blocking, and `mypy --strict` is not yet wired into CI (there is no CI configured at all yet). Most of this codebase predates this rule — see `docs/ruff-baseline-report.md` for the actual gap count (roughly 75 violations inside `src/urolens/` specifically, once test files and scripts are excluded from the raw total).

## 11. Schemas only in `schemas/`

Grouped by domain module, exported via `schemas/__init__.py` (with the two documented exceptions above). No inline `BaseModel`/dict shape in a router or service — this was itself a violation found and fixed in `api/notifications.py` and `api/results.py` (see `changelog.md`'s "Inline Pydantic schemas outside schemas/" entry); don't reintroduce it.

## 12. No raw exceptions to the client

Never `detail=str(e)`. Log server-side, return a generic message plus the shared envelope (rule 8) plus an internal reference ID if useful for support/debugging.

## 13. Migrations

`psycopg2` for Alembic's migration runner, `asyncpg` for the app's runtime queries — this is why `psycopg2-binary` is a real, required dependency even though no application code imports it by name (SQLAlchemy resolves the driver from the connection-string scheme). RLS disabled per-table must be explicit in the migration and compensated for by rule 2 — verify it holds before shipping the table. Use `op.execute()` for triggers. Always run `alembic heads` before authoring a new migration — single head only. No `CREATE DATABASE`/`CREATE USER`/`GRANT`/`\c` in any migration SQL. No out-of-band hand-applied SQL, ever — if it's not in a migration, it doesn't exist as far as this codebase's history is concerned. The migration filename must match its embedded revision ID.

## 14. One implementation per feature

Reimplementing or moving a feature means deleting the old one in the *same* PR. Nothing unregistered/unmounted left "just in case." An unreferenced router/service/model gets deleted, not kept around — this rule was applied repeatedly during the backend consolidation (see `changelog.md`) and again when the duplicate lab-request-creation implementations (one Supabase-REST, one SQLAlchemy — one used by the physician route, one by the receptionist route) were merged into a single implementation.

## 15. Tests by risk tier

- **Tier 1** (confirm → override → approve → release chain, auth/session/RBAC): unit + integration tests required, no reduced coverage.
- **Tier 2** (PHI CRUD): unit tests for validation + access control.
- **Tier 3** (internal/utility): tests where the logic is non-trivial.

A test whose signature no longer matches its target is a CI failure to fix, not something to silently skip. See `tests/test_lab_request_service.py` for a recent example of the expected pattern: characterization tests written *before* a refactor to pin existing behavior, then rewritten to assert the new behavior once the refactor landed.

## 16. Dependencies

Pin external/private dependencies to an exact version (or a specific tag/commit for a git dependency) — never a floating spec, never a mutable branch. `requirements.txt` is this project's sole dependency manifest (there is no `pyproject.toml`-based dependency management, and no dev/prod split — `ruff`, `pytest`, and `pytest-asyncio` live in the same file as `fastapi`). UTF-8 everywhere; `requirements.txt` was previously UTF-16-encoded (and, on top of that, partially re-saved in UTF-8 at some point without fixing the rest of the file) — fixed, verified via hex dump.

**Known open violation, not yet fixed**: `urolens-ai-engine @ git+https://github.com/urolens-project/urolens-ai-engine.git@develop` pins to a mutable branch rather than a tag or commit. Flagged repeatedly (see `changelog.md`) rather than fixed, since correcting it needs a specific commit/tag decision from whoever owns that dependency, not something to guess at while doing unrelated work.

## Keeping this document accurate

Several sections above ("Current state" / "Known open violation") describe a snapshot, not a permanent guarantee. If a change you're making alters one of these — you migrate call sites onto a barrel, you close the `urolens-ai-engine` pin, you wire up CI — update the relevant section here in the same PR, the same way `changelog.md` gets updated for every change in this codebase.
