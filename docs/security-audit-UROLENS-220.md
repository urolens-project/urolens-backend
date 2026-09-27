# Security audit — backend for mobile (UROLENS-220, SEC-1)

| | |
|---|---|
| **Ticket** | UROLENS-220 "Mobile" (subtask of UROLENS-80 *[Security & Compliance] Supabase Integration*, epic UROLENS-66) |
| **Date** | 2026-09-27 |
| **Scope** | `urolens-backend` routes used by the mobile app, the Supabase project behind them, and Python dependencies |
| **Method** | Manual review against the security-auditor checklist (access control, injection, auth/sessions, input validation, secrets, transport, errors/logging, availability); `pip-audit` on `requirements.txt`; secret scan of all four repos' full git history; live Supabase queries run by the ticket owner |
| **Type** | Read-only. Nothing in this report was changed by the audit itself; fixes land as separate commits (see *Fix plan*) |

## Verdict

**2 critical/high issues found and already fixed on this branch** (F-01, F-02).
**Open: 4 high, 7 medium, 8 low.** The highs should be closed before the next
production deploy; all but F-06 (which may belong to UROLENS-81) fit in SEC-2.

| Severity | Open | Fixed on branch |
|---|---|---|
| 🔴 Critical | 0 | 1 |
| 🟠 High | 4 | 1 |
| 🟡 Medium | 7 | 0 |
| 🔵 Low | 8 | 0 |

The pattern behind most open highs: **write endpoints the mobile app uses don't
check that the specimen belongs to the calling MedTech, or that the result is
still editable.** `rejectSpecimen` and `startAnalysis` already do this correctly
([specimen_service.py:255](../src/services/specimen_service.py#L255),
[:323](../src/services/specimen_service.py#L323), 403 `SPECIMEN_NOT_ASSIGNED`);
upload, discard, confirm and override don't.

## Attack surface covered

| Router | Mobile routes | Guard | Notes |
|---|---|---|---|
| `auth` | `POST /login`, `POST /logout` | public / `getCurrentUser` | F-06, F-07 |
| `sync` | `GET /sync/pull` | `getCurrentUser` | Scoped to caller's `medtech_id`; F-20 |
| `images` | `POST /images/upload`, `POST /images/{id}/discard` | `RequireRole([MEDTECH])` | F-04, F-05, F-08, F-09 |
| `results` | `POST /results/{id}/confirm`, `POST /results/{id}/override`, `GET /results/medtech/pending` | MEDTECH (override: MEDTECH or SUPERVISOR) | F-03, F-08 |
| `specimens` | `POST /specimens/{id}/reject`, `POST /specimens/{id}/start-analysis` | MEDTECH | Ownership enforced ✅ |
| `notifications` | list, mark read, read-all, push token | `getCurrentUser` | Owner-scoped ✅ |

**Trust boundaries:** web and mobile reach data only through this backend
(verified: no client uses the Supabase anon key or `supabase-js`, in any branch or
commit). The backend reaches Postgres as the table owner (SQLAlchemy/Alembic) and
through Supabase REST with the service-role key.

**Sensitive data:** patient identity and demographics (Fernet-encrypted at rest),
urine microscopy images, lab results, bcrypt password hashes, session rows, audit
logs. All of it is personal/sensitive personal information under RA 10173.

---

## Findings

### ✅ F-01 🔴 Critical — Every table was readable and writable with the anon key *(fixed: SEC-0)*
All 27 `public` tables had RLS disabled; Supabase grants `anon`/`authenticated`
full table privileges by default. Anyone with the project's anon key (public by
design) could read, change or delete every row — patients, password hashes,
sessions, audit logs — through PostgREST, bypassing the backend.
**Fix:** migration `0040` enables RLS on every table with no policies; the backend
is unaffected (owner and service role bypass RLS). Verified live by the ticket owner;
`scripts/check_rls.py` and `tests/test_rls_migration.py` keep it that way.

### ✅ F-02 🟠 High — Microscopy images were public by link *(fixed: SEC-0b, pending live apply)*
The `microscopy` bucket was `public = true` and the supervisor-review and
physician-result details returned permanent `/object/public/` links: no login,
no expiry, no revocation.
**Fix:** 1-hour signed URLs via `src/core/storage.signedImageUrl`; migration `0041`
makes the bucket private and limits it to JPEG/PNG. Deploy code with `0041`.

### F-03 🟠 High — A released result can still be changed by a manual override
**File:** [manual_override_service.py:67](../src/services/manual_override_service.py#L67)
**Issue:** the only status guard is `result.status == APPROVED`. `RELEASED`,
`PENDING_SUPERVISOR_APPROVAL` and `CRITICAL_ESCALATED` results are all
overridable, and there is no check that the caller is assigned to the specimen.
**Impact:** any MedTech can change a parameter on a result that has already been
released to the patient and physician, or that a supervisor is reviewing — silent
tampering with a finalised clinical record.
**Fix:** allow-list the editable statuses and add the ownership check:
```python
_OVERRIDABLE_STATUSES = {ResultStatus.PENDING_CONFIRM, ResultStatus.RETURNED_FOR_CORRECTION}

if result.status not in _OVERRIDABLE_STATUSES:
    raise UnprocessableException(
        code="RESULT_ALREADY_FINALISED",
        message="This result can no longer be changed.",
    )
```
Ownership for MEDTECH callers: same check as `specimen_service.startAnalysis`
(403 `SPECIMEN_NOT_ASSIGNED`); SUPERVISOR callers stay unrestricted.

### F-04 🟠 High — Image upload doesn't check the specimen's owner or status
**File:** [ai_integration_service.py:102-108](../src/services/ai_integration_service.py#L102-L108), reset at [:249](../src/services/ai_integration_service.py#L249)
**Issue:** `handleUpload` never loads the specimen. Any MedTech can upload an image
for any specimen ID, and the upload resets that specimen's result to
`PENDING_CONFIRM` and wipes `aiFindings`/`flaggedAnomalies` — regardless of status.
**Impact:** any MedTech can undo an approved or released result, or attach an
image to a rejected specimen, for a specimen assigned to someone else. A
non-existent specimen ID stores the file in the bucket first, then fails the
`images` foreign key with a 500 (orphaned object).
**Fix:** before reading the file, load the specimen and reject:
404 if missing, 403 `SPECIMEN_NOT_ASSIGNED` if `specimen.medtechId != uploaderId`,
409 `SPECIMEN_REJECTED` if rejected, 409 if the result is past
`RETURNED_FOR_CORRECTION`.

### F-05 🟠 High — Upload parses any image format with a vulnerable Pillow
**Files:** [ai_integration_service.py:171](../src/services/ai_integration_service.py#L171), [requirements.txt:35](../requirements.txt#L35) (`Pillow==12.2.0`)
**Issue:** `PILImage.open()` is called without `formats=`, so Pillow sniffs the
bytes and runs whichever parser matches. The only format check is the
client-supplied `Content-Type`. Pillow 12.2.0 has 15 advisories fixed in 12.3.0,
several in parsers reachable from `Image.open` (EPS negative seek
CVE-2026-59203, GD decompression bomb CVE-2026-55380, JPEG2000 tile overflow
CVE-2026-59204).
**Impact:** anyone holding a MedTech token can send an EPS/GD/JPEG2000 file labelled
`image/jpeg` and reach those parsers — memory exhaustion or native heap
corruption in the API process.
**Fix:** both, not either:
```python
img = PILImage.open(io.BytesIO(rawBytes), formats=["JPEG", "PNG"])
```
plus `Pillow==12.3.0`, and reject when `img.format` doesn't match the declared type.

### F-06 🟠 High — No rate limiting on staff or patient login
**Files:** [auth.py:37](../src/api/auth.py#L37), [patient_auth.py:11](../src/api/patient_auth.py#L11)
**Issue:** no rate limiter anywhere in the app. The per-account lockout (5 failures)
doesn't slow down attempts spread across many usernames.
**Impact:** unlimited password guessing across accounts, and each attempt costs a
~300 ms bcrypt check on the server (cheap CPU exhaustion).
**Fix:** per-IP and per-username limits on both login routes (e.g. `slowapi`,
10/min per IP); return 429 with the standard error envelope.

### F-07 🟡 Medium — Login reveals which usernames exist, and anyone can lock them
**Files:** [auth.py:58-63](../src/api/auth.py#L58-L63), [auth_service.py:71](../src/core/auth_service.py#L71)
**Issue:** an unknown username returns immediately; a real one runs bcrypt first
(~300 ms), so response time tells an attacker which usernames exist. Five wrong
passwords then lock that account until an administrator unlocks it. The counter
is read-then-written, so parallel attempts can exceed 5 before the lock applies.
**Impact:** targeted denial of service against named staff (lock out every MedTech
before a shift), and more than 5 guesses per account.
**Fix:** run a dummy `bcrypt.checkpw` against a fixed hash when the user doesn't
exist; make the lock time-based (e.g. 15 minutes) instead of admin-only; increment
atomically (`UPDATE users SET failed_attempts = failed_attempts + 1 ... RETURNING`).
Same review for `patient_auth_service.patientLogin`.

### F-08 🟡 Medium — Any MedTech can confirm another's result or discard another's image
**Files:** [result_confirmation_service.py:106](../src/services/result_confirmation_service.py#L106), [image_retake_service.py:51](../src/services/image_retake_service.py#L51)
**Issue:** status guards exist, ownership checks don't. Discard also works on an
image whose result is already approved or released.
**Impact:** a MedTech can push a colleague's result to the supervisor under their
own name, or discard the active image behind a finalised result.
**Fix:** the `SPECIMEN_NOT_ASSIGNED` check in both; discard only while the result is
`PENDING_CONFIRM` or `RETURNED_FOR_CORRECTION`.

### F-09 🟡 Medium — No upload size limit
**File:** [ai_integration_service.py:102](../src/services/ai_integration_service.py#L102)
**Issue:** `await file.read()` loads the whole upload into memory; neither the app
nor the bucket has a size cap (bucket limit deliberately left unset in `0041` until
this is fixed).
**Impact:** a few concurrent multi-GB uploads exhaust the API's memory.
**Fix:** reject on `Content-Length` over a cap (proposal: 15 MB, covers
full-resolution phone photos) and read with a bounded loop; then set the matching
bucket `file_size_limit`.

### F-10 🟡 Medium — Starlette 1.0.0: form-parsing limits skipped before auth runs
**File:** [requirements.txt:29](../requirements.txt#L29)
**Issue:** 5 advisories. CVE-2026-54283: `max_fields`/`max_part_size` are ignored
for `application/x-www-form-urlencoded`. FastAPI parses the form body of
`POST /images/upload` before resolving the auth dependency, so this is reachable
without a token *(judgment from FastAPI's request flow, not tested)*. The Host-header
advisories (CVE-2026-48710, -54282) are not reachable: no code uses `request.url`.
**Fix:** `starlette==1.3.1` (FastAPI 0.136.1 accepts `starlette>=0.46`); run the
suite.

### F-11 🟡 Medium — A failed audit-log write is silently dropped
**File:** [audit_logger.py:57](../src/core/audit_logger.py#L57)
**Issue:** `AuditLogger.record` catches every exception and only logs it. The design
(never break the main transaction) is sound; the silence isn't.
**Impact:** RA 10173 accountability depends on a complete audit trail. An outage
of the audit write loses evidence with nothing but a log line.
**Fix:** part of SEC-3 — alert on failures (metric or error-level log that's
monitored), and consider writing audit rows in the same DB transaction for the
SQLAlchemy paths.

### F-12 🟡 Medium — AI engine is installed from a moving branch
**File:** [requirements.txt:39](../requirements.txt#L39) (`urolens-ai-engine @ git+...@develop`)
**Issue:** breaks the CONTRIBUTING rule "never a mutable branch". Every install
pulls whatever `develop` is at that moment; `pip-audit` can't scan it.
**Impact:** any merge to `develop` in another repo changes production code on the
next build, unreviewed from this repo's side.
**Fix:** pin a tag or commit SHA (`...@<sha>`) and bump it deliberately.

### F-13 🟡 Medium — `.env` silently overrides real environment variables
**Files:** [config.py:38](../src/core/config.py#L38), [main.py:3](../main.py#L3)
**Issue:** `load_dotenv(override=True)`: if a `.env` file exists, its values win
over the process environment.
**Impact:** a stray `.env` in a deployed image, or on a developer machine,
redirects the app, Alembic or `scripts/check_rls.py` to a different database than
the one set in the environment — this nearly pointed an audit test run at the live
database.
**Fix:** `override=False` in both places (environment wins, `.env` fills gaps).
SEC-5.

### F-14 🔵 Low — PyJWT 2.12.1 advisories (not reachable)
**File:** [requirements.txt:23](../requirements.txt#L23). 5 advisories fixed in 2.13.0.
Not reachable: only HS256 is accepted (`algorithms=[settings.jwtAlgorithm]`), and
`PyJWKClient` and detached-payload JWS aren't used. **Fix:** bump to 2.13.0 anyway.

### F-15 🔵 Low — cryptography 48.0.0 advisories
**File:** [requirements.txt:10](../requirements.txt#L10). Only Fernet is used; the X.509
and PKCS#7 advisories are not reachable. GHSA-537c-gmf6-5ccf is in the bundled
OpenSSL (fixed 48.0.1). **Fix:** bump to 48.0.1 now; 50.0.0 after testing.

### F-16 🔵 Low — anyio 4.13.0 and python-dotenv 1.2.1 advisories (not reachable)
No process pools, no TLS with non-ASCII hostnames, no `set_key`/`unset_key`.
**Fix:** anyio 4.14.2, python-dotenv 1.2.2 in the same patch batch.

### F-17 🔵 Low — No security headers; API docs served in production
**File:** [main.py:34](../main.py#L34)
**Issue:** no `X-Content-Type-Options`, `Strict-Transport-Security`,
`Cache-Control: no-store` on patient data, no request body limit, and `/docs` +
`/openapi.json` are public, publishing the full route map.
**Fix:** SEC-5 — headers middleware; `docs_url=None, openapi_url=None` when
`APP_ENV=production` (keep CI's OpenAPI check on a non-production setting).

### F-18 🔵 Low — Upload error echoes Pillow's internal exception text
**File:** [ai_integration_service.py:177](../src/services/ai_integration_service.py#L177)
Violates rule 12 (no raw exception text to the client). **Fix:** fixed message, log the
exception server-side. Falls out of the F-05 change.

### F-19 🔵 Low — Sync route doesn't restrict the role
**File:** [sync.py:19](../src/api/sync.py#L19)
Uses `getCurrentUser` rather than `RequireRole([MEDTECH])`. No data leak (the query
is scoped to the caller's `medtech_id`, so other roles get empty lists), but it's the
one mobile route off the rule-2 pattern. A full sync is also unbounded (all of a
MedTech's history) — performance, tracked in SEC-4.

### F-20 🔵 Low — `jwtExpiryHours` setting does nothing
**File:** [config.py:62](../src/core/config.py#L62)
Tokens actually expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (60). The unused setting
suggests an 8-hour lifetime to anyone reading the config. **Fix:** remove it.

### F-21 🔵 Low — Storage upload failure still saves the image row
**File:** [ai_integration_service.py:112](../src/services/ai_integration_service.py#L112)
Upload failure is logged and ignored by design, so a result can reference an image
that doesn't exist. Now visible as `imageUrl: null` (SEC-0b) rather than a broken
link; decide in SEC-5 whether to fail the upload instead.

---

## Dependency scan

`pip-audit` on the pinned `requirements.txt` (full dependency tree): **30 unique
advisories in 6 direct dependencies, none in transitive ones.**

| Package | Installed | Fixed | Advisories | Reachable? | Finding |
|---|---|---|---|---|---|
| Pillow | 12.2.0 | 12.3.0 | 15 | **Yes** — upload parser | F-05 🟠 |
| starlette | 1.0.0 | 1.3.1 | 5 | **Likely** (form DoS); Host-header: no | F-10 🟡 |
| PyJWT | 2.12.1 | 2.13.0 | 5 | No | F-14 🔵 |
| cryptography | 48.0.0 | 48.0.1 / 50.0.0 | 4 | Bundled OpenSSL only | F-15 🔵 |
| anyio | 4.13.0 | 4.14.2 | 2 | No | F-16 🔵 |
| python-dotenv | 1.2.1 | 1.2.2 | 1 | No | F-16 🔵 |
| urolens-ai-engine | `@develop` | — | not scannable | — | F-12 🟡 |

Suggested batches: (1) Pillow + starlette + PyJWT + cryptography 48.0.1 + anyio +
python-dotenv — all patch/minor, one commit, full suite; (2) cryptography 50.0.0 on
its own after reading the changelog.

## Secrets scan

All four repos (`urolens-backend`, `-mobile`, `-web`, `-ai-engine`), every branch,
full history: **no real secrets found.** The only key-shaped strings are PyJWT's
documentation example token (committed with `venv/`, since removed) and a fake token
in `urolens-web/e2e/auth.spec.ts`. Only `.env.example` files were ever committed.
All four repos are **public**, so this needs to stay true — see *Recommendations*.

## What's already good

- Every route has an auth dependency; role checks are server-side (`RequireRole`).
- Passwords: bcrypt, checked in a worker thread. JWT: algorithm pinned, verified with
  `jwt.decode`, 60-minute expiry, and **server-side session revocation checked on
  every request**.
- Patient identity fields encrypted at rest (Fernet); decrypt failures are logged by
  row ID only — no patient data found in any log statement.
- The app refuses to start with a missing secret or the placeholder JWT key.
- CORS: `*` with `allow_credentials=False`, correct for Bearer tokens.
- Consistent error envelope; unhandled errors don't return stack traces.
- Sync, notifications, specimen reject/start-analysis are correctly scoped to the
  caller. Mobile list endpoints cap `pageSize` at 100.
- Mobile stores tokens in the Keychain/Keystore (`expo-secure-store`).

## Not covered

- Supabase project settings beyond RLS and Storage (auth providers, network
  restrictions, backups, PITR), hosting/infrastructure, TLS termination.
- Web-only routes (receptionist, supervisor, physician, patient portal) — UROLENS-221.
  One note for that team: `GET /api/v1/results/approved` takes an uncapped `limit`
  ([result_releasing.py:43](../src/api/result_releasing.py#L43)).
- The AI engine's own code (separate repo; also not scannable here — F-12).
- Mobile client code beyond token storage and Supabase usage; accessibility (SEC-7).
- Dynamic testing: nothing here was exploited against a running server. Findings are
  from code reading; F-10's reachability is a judgment.

## Fix plan

| Finding | Where it gets fixed | Status |
|---|---|---|
| F-01, F-02 | SEC-0, SEC-0b (`feat/UROLENS-220-supabase-security-compliance`) | ✅ Fixed |
| F-03, F-04, F-08 | **SEC-2** (`fix/UROLENS-220-sec-2-access-control-upload-hardening`) — ownership + editable-status guards (`services/specimen_access.py`), specimen row lock | ✅ Fixed |
| F-05, F-09, F-18 | **SEC-2** — upload hardening (JPEG/PNG decoders only, 10 MB cap, migration `0042`) | ✅ Fixed |
| F-10, F-14, F-16 | **SEC-2** — dependency batch 1 | ✅ Fixed |
| F-15 | SEC-2 took cryptography to 48.0.1 (OpenSSL fix); the X.509/PKCS#7 advisories need 50.0.0 | 🟡 Partly — batch 2 |
| F-06, F-07 | SEC-2, or UROLENS-81 (JWT/RBAC hardening) — confirm with its owner | ⏳ Pending that decision |
| F-12 | Pin the AI engine commit (one line; coordinate with the AI engine owner) | 🔜 Planned |
| F-11 | SEC-3 (`feat/UROLENS-220-sec-3-ra10173-controls`) | Next sprint |
| F-13, F-17, F-21 | SEC-5 (`feat/UROLENS-220-sec-5-production-readiness`) | Partly Sprint 5 |
| F-19, F-20 | SEC-5 cleanup | Next sprint |

## Recommendations beyond this ticket

- Add `pip-audit -r requirements.txt` to CI (report-only first), and enable GitHub
  Dependabot alerts on all four repos.
- Because the repos are public, add a secret scanner (e.g. `gitleaks`) as a
  pre-commit hook and CI step.
- Keep running `python -m scripts.check_rls` after every migration, per rule 13.
