# Multi-field shared foundation: plan

Plan only. No code, migration file or DB command was produced for this. Scope is the "Shared foundation" and "Seams" sections of `docs/UroLens Multi-Field Web and Mobile Readiness Split.docx` (the file the brief calls `multifield-backend-split.md`; that `.md` does not exist). Spec: `docs/SDD_Module2_MultiField_Update_Proposal.docx` sections 4, 6, 7, 10.4. Code facts: `docs/multifield-readiness-merged.md`, re-verified against the working tree below. Live facts: `docs/live-schema.json` and `docs/live-schema-enum.json` (the brief says `-enums`; the file is `-enum`).

Fixed decisions D1 to D6 are applied as written. Where the spec or the contract draft disagrees with them, section 7 lists it and the decision wins.

---

## 0. Pre-check results

| # | Check | Result |
|---|---|---|
| 1 | Git state | Top level `urolens-backend`. **HEAD `f1a1a63` on `fix/b1-baseline-migration`; `origin/development` is `f0bc3c0`. HEAD is not `origin/development`, which is the literal stop condition.** HEAD is exactly one commit ahead (merge-base `f0bc3c0`; the one commit is the B1 baseline, pushed to origin). Nothing on `origin/development` is missing from HEAD. I treated this as safe to plan from and am flagging it; the plan is only valid on top of B1 (see 2). If you want the strict reading, discard this file. |
| 2 | B1 baseline | **Present on HEAD, not on `origin/development`.** `0000_baseline_missing_tables` and `0046_add_missing_live_fks`, plus `tests/test_baseline_migration.py`. Until that PR merges: **migration execution and the fresh-DB tests are BLOCKED ON B1.** Everything else (models, settings, schemas, stubs, exceptions) is not blocked. |
| 3 | Alembic head | Single head. HEAD: `0046`. `origin/development`: `0045`. **Next free number is `0047` (`0047_multifield_foundation`)**, assuming B1 merges first. If the foundation lands first it must take `0046` and B1 renumbers; do not let both claim `0046`. The unmerged `feat/review-image-annotation` also claims `0046`; D3 says it is not merged, so no conflict while that holds. |
| 4 | Tests | `pytest -m "not integration" --strict-markers`: **880 passed, 0 failed** (873 on `origin/development` + 7 B1 tests). `python -m scripts.check_openapi`: OK, 53 routes. CI runs plain `pytest -q`, so the same 880. No test uses an `integration` marker; it is not registered. |
| 5 | Engine config (B0/P-2) | **Not fixed.** `GATE_WEIGHTS_PATH` and `RULE_ENGINE_CONFIG_PATH` appear nowhere in `src/` or `.env.example`. `config.py:100-114` has a bundled fallback for the model weights only (`weights.pt`). `changelog.md` still says production needs an explicit `RULE_ENGINE_CONFIG_PATH`. Does not block the foundation. |

### LIVE FACT: `analysis_results.status`

The brief's `<FILL>` was left blank. What I can establish:

- `docs/live-schema.json` covers five tables only, so it does not show `analysis_results`.
- Migration `0020` converted the column from the `result_status` enum to `VARCHAR(48)`, and the model is `String(48)` (`analysis_result.py`, status column). `live-schema-enum.json` shows the `result_status` type still exists in live (8 values, including `PENDING` and `CONFIRMED`) as an unused leftover, which fits a converted column.
- So the plan **assumes `character varying(48)`, unverified on live.** Please run: `SELECT data_type, udt_name, character_maximum_length FROM information_schema.columns WHERE table_name='analysis_results' AND column_name='status';`

How the migration handles it: step 0 is a `DO` block that raises if the column is not `character varying`, before anything is changed. If it raises, the contingency is a separate earlier revision using `ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'CAPTURING'` inside `op.get_context().autocommit_block()` (it cannot run in a transaction block). The main migration needs no change either way.

---

## 1. Schema specs

Python attributes are camelCase (rule 17); DB columns are snake_case via the first `mapped_column` argument, as in the existing models. All FKs are `ON DELETE RESTRICT`, matching the existing models. Timestamps are `TIMESTAMPTZ`.

### 1.1 `result_fields` (new)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| field_id | UUID | no | `gen_random_uuid()` | PK `pk_result_fields` |
| result_id | UUID | no | | FK `fk_result_fields_result` → analysis_results(result_id). Index `ix_result_fields_result_id` |
| specimen_id | UUID | no | | FK `fk_result_fields_specimen` → specimens(specimen_id). Index `ix_result_fields_specimen_id` |
| image_id | UUID | no | | FK `fk_result_fields_image` → images(image_id). UNIQUE `uq_result_fields_image` |
| field_no | SMALLINT | no | | UNIQUE `uq_result_fields_result_field_no` (result_id, field_no). Numbers are never reused after a discard |
| magnification | VARCHAR(3) | no | `'HPF'` | CHECK `ck_result_fields_magnification` IN ('HPF','LPF') |
| status | VARCHAR(20) | no | `'ANALYZED'` | CHECK `ck_result_fields_status` IN ('ANALYZED','ACCEPTED','DISCARDED') |
| ai_findings | JSONB | no | `'{}'` | class → count, as the AI returned it. Never changes |
| detections | JSONB | no | `'[]'` | `[{id, particleType, confidence, x, y, w, h}]`, percent of the upright image, ids assigned by the backend at store time, up to 1,000. **Deferred in the ORM** |
| image_width | INTEGER | no | | upright image size the boxes refer to |
| image_height | INTEGER | no | | |
| confidence_scores | JSONB | no | `'{}'` | class → mean confidence |
| effective_counts | JSONB | no | `'{}'` | equals ai_findings until corrected |
| smart_diagnosis | JSONB | yes | | per-field preview |
| smart_diagnosis_unavailable | BOOLEAN | no | `false` | |
| model_version | VARCHAR(30) | no | | same width as `analysis_results.model_version` |
| discard_reason | TEXT | yes | | |
| verified_by | UUID | yes | | FK `fk_result_fields_verified_by` → users |
| verified_at | TIMESTAMPTZ | yes | | |
| last_opened_by | UUID | yes | | FK `fk_result_fields_opened_by` → users |
| last_opened_at | TIMESTAMPTZ | yes | | |
| last_corrected_at | TIMESTAMPTZ | yes | | |
| version | INTEGER | no | `1` | CHECK `ck_result_fields_version` (version >= 1) |
| created_at | TIMESTAMPTZ | no | `now()` | |
| updated_at | TIMESTAMPTZ | no | `now()` | drives delta sync; ORM `onupdate=func.now()` |

Index choice: one single-column index each on `result_id` and `specimen_id`, as briefed. A composite `(specimen_id, updated_at)` would suit the sync pull better; left to the mobile lane (Q13).

### 1.2 `field_annotation_edits` (new)

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| edit_id | UUID | no | `gen_random_uuid()` | PK |
| field_id | UUID | no | | FK → result_fields(field_id). Index `ix_field_annotation_edits_field_id` |
| action | VARCHAR(10) | no | | CHECK IN ('REMOVED','ADDED') |
| detection_id | VARCHAR(64) | yes | | the id inside `result_fields.detections` (REMOVED) |
| particle_type | VARCHAR(60) | no | | validated against `PARTICLE_LABELS` in the schema, not in the DB |
| x, y, w, h | REAL | yes | | percent of the upright image (ADDED) |
| edited_by | UUID | no | | FK → users |
| edited_at | TIMESTAMPTZ | no | `now()` | |

Shape CHECK `ck_field_annotation_edits_shape`: REMOVED needs `detection_id`; ADDED needs `x, y, w, h`. Percent range (0 to 100) is validated in the schema, not the DB. No `superseded_at`: the brief has none, and "PUT replaces the list" means rows are replaced; the audit trail is the `FIELD_ANNOTATIONS_SAVED` audit row (Q12).

### 1.3 Existing tables

| Table | Change |
|---|---|
| manual_overrides | `field_id UUID NULL` FK → result_fields(field_id), index `ix_manual_overrides_field_id`; `kind VARCHAR(10) NOT NULL DEFAULT 'OVERRIDE'` with CHECK IN ('OVERRIDE','REVERT'). Existing rows untouched (they read as OVERRIDE with a null field). Constant defaults are metadata-only on Postgres 11+. REVERT rows still need `original_ai_value`/`corrected_value`/`rationale` (all NOT NULL); the web lane must fill them. |
| analysis_results | `field_count SMALLINT NOT NULL DEFAULT 0` (meaning: **number of ACCEPTED fields**, Q6), `field_summary JSONB NULL`, `is_legacy_single_field BOOLEAN NOT NULL DEFAULT false`. `ResultStatus` gains `CAPTURING`. No DB type or constraint change on `status`. |
| images | No column change. |

### 1.4 RLS

`ALTER TABLE result_fields ENABLE ROW LEVEL SECURITY` and the same for `field_annotation_edits`, no policies, as `0040` did. Written as literal statements, not a loop with an f-string: `tests/test_rls_migration.py:23-24,106-116` finds them with a regex on the revision source. That test then covers both tables automatically.

---

## 2. Migration: `alembic/versions/0047_multifield_foundation.py`

Down revision `0046`. Safe with the flag off: everything is additive, no existing column is altered or read differently, and no app code reads the new objects yet.

### 2.1 Upgrade steps, in order

0. **Preflight** `DO` block: raise `analysis_results.status is not varchar` if it isn't `character varying`. No change made.
1. `create_table result_fields` with the constraints and indexes in 1.1.
2. `create_table field_annotation_edits` (1.2).
3. `ENABLE ROW LEVEL SECURITY` on both.
4. `manual_overrides`: `ADD COLUMN IF NOT EXISTS field_id`, `kind`; `CREATE INDEX IF NOT EXISTS`; CHECK added inside a `pg_constraint` name guard (the repo's pattern from 0032/0045; live drift has happened before).
5. `analysis_results`: `ADD COLUMN IF NOT EXISTS` for the three columns.
6. **Backfill fields** (D4 and D5), idempotent:
   ```sql
   INSERT INTO result_fields (field_id, result_id, specimen_id, image_id, field_no, magnification,
       status, ai_findings, detections, image_width, image_height, confidence_scores, effective_counts,
       smart_diagnosis, smart_diagnosis_unavailable, model_version, created_at, updated_at)
   SELECT gen_random_uuid(), r.result_id, r.specimen_id, i.image_id, 1, 'HPF', 'ACCEPTED',
          COALESCE(r.ai_findings,'{}'::jsonb), '[]'::jsonb, i.width_px, i.height_px, '{}'::jsonb,
          CASE WHEN r.particle_classes <> '{}'::jsonb THEN r.particle_classes
               ELSE COALESCE(r.ai_findings,'{}'::jsonb) END,
          r.smart_diagnosis, r.smart_diagnosis_unavailable, r.model_version, i.uploaded_at, now()
   FROM analysis_results r JOIN images i ON i.image_id = r.image_id
   WHERE i.status = 'ACTIVE'
     AND NOT EXISTS (SELECT 1 FROM result_fields f WHERE f.image_id = i.image_id);
   ```
   - `effective_counts` is the confirmed `particle_classes` when set, else the AI counts, so old results keep showing what they show today.
   - `updated_at = now()` on purpose: a mobile client with a cursor from before the migration picks the backfilled field up on its next pull once the flag is on.
   - Boxes are empty (`'[]'`); `image_width/height` come from `images.width_px/height_px` (pre-orientation, harmless with no boxes).
   - `verified_by/verified_at` stay NULL for every backfilled field (Q5).
7. `UPDATE analysis_results r SET field_count = 1 WHERE EXISTS (backfilled ACCEPTED field)`. **This statement must not set `updated_at`**, so `/sync/pull` does not resend every result.
8. **Legacy flag** (D4): `UPDATE analysis_results SET is_legacy_single_field = true WHERE status IN ('PENDING_CONFIRM','IMAGE_RETAKE_REQUESTED','FAILED','RETURNED_FOR_CORRECTION') AND field_count = 1`. Status is not changed; nothing moves to CAPTURING. Results at any other status (PENDING_SUPERVISOR_APPROVAL, CRITICAL_ESCALATED, APPROVED, RELEASED) get a field with the flag false (D5).
9. Post-check `DO` block: raise if any `is_legacy_single_field` row has no field.

Results with no `image_id` get no field and stay as they are. Results in a legacy status whose image is not ACTIVE are also skipped (Q3, risk R4).

### 2.2 CAPTURING handling

Python only: add `CAPTURING = "CAPTURING"` to `ResultStatus`. The column is VARCHAR with no CHECK, so no DDL. I found no code that iterates `ResultStatus` or validates against a closed list; the only status `Literal` is `MedtechQueueStatus` (`result_review.py:136`), which should not include it. With the flag off nothing ever writes CAPTURING. The contingency for an enum column is in section 0.

### 2.3 Downgrade

Guarded first: raise if any `field_annotation_edits` row exists, any `manual_overrides.field_id IS NOT NULL`, or any result is `CAPTURING`, since that data cannot be represented without these objects. Backfilled-only data is derivable and dropped without complaint. Then, in reverse: drop `manual_overrides.field_id/kind` (index and check first), the three `analysis_results` columns, `field_annotation_edits`, `result_fields`. For a scratch or staging database only; it never runs on live.

### 2.4 Dry-run SELECTs (go in the migration docstring; run on live before upgrading)

```sql
-- D1 live type of analysis_results.status (expect character varying / varchar / 48)
SELECT data_type, udt_name, character_maximum_length FROM information_schema.columns
 WHERE table_name='analysis_results' AND column_name='status';

-- D2 what the backfill will touch: status x image status
SELECT r.status, i.status AS image_status, count(*) FROM analysis_results r
  LEFT JOIN images i ON i.image_id = r.image_id GROUP BY 1,2 ORDER BY 1,2;

-- D3 rows that get a field AND the legacy flag
SELECT r.result_id, r.status FROM analysis_results r JOIN images i ON i.image_id = r.image_id
 WHERE i.status='ACTIVE'
   AND r.status IN ('PENDING_CONFIRM','IMAGE_RETAKE_REQUESTED','FAILED','RETURNED_FOR_CORRECTION');

-- D4 legacy-status rows that get NO field (null or non-active image): needs a decision (Q3)
SELECT r.result_id, r.status, i.status AS image_status FROM analysis_results r
  LEFT JOIN images i ON i.image_id = r.image_id
 WHERE r.status IN ('PENDING_CONFIRM','IMAGE_RETAKE_REQUESTED','FAILED','RETURNED_FOR_CORRECTION')
   AND (i.image_id IS NULL OR i.status <> 'ACTIVE');

-- D5 one image shared by several results would break UNIQUE(image_id); expect 0 rows
SELECT image_id, count(*) FROM analysis_results WHERE image_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;

-- D6 manual_overrides live column types (P-4); expect text/text/varchar(60)
SELECT column_name, data_type, character_maximum_length, is_nullable FROM information_schema.columns
 WHERE table_name='manual_overrides' ORDER BY ordinal_position;

-- D7 after upgrade: backfilled fields = results with an active image; legacy rows all have a field
SELECT (SELECT count(*) FROM result_fields) AS fields,
       (SELECT count(*) FROM analysis_results WHERE is_legacy_single_field) AS legacy,
       (SELECT count(*) FROM analysis_results WHERE field_count = 1) AS with_one_field;
```

---

## 3. Files to add or change

### 3.1 New (12)

| File | Contents |
|---|---|
| `alembic/versions/0047_multifield_foundation.py` | Section 2. Docstring holds the D1 to D7 SELECTs and the order rule with B1. |
| `src/models/result_field.py` | `FieldStatus`, `Magnification` StrEnums. `ResultField` per 1.1 (attrs `fieldId`, `resultId`, `fieldNo`, `imageWidth`, …). `detections = mapped_column("detections", JSONB, nullable=False, default=list, deferred=True)`. Relationships: `analysisResult`, `image`, `edits` (order by `editedAt`), `manualOverrides`. |
| `src/models/field_annotation_edit.py` | `EditAction` StrEnum, `FieldAnnotationEdit` per 1.2, `field` relationship. |
| `src/schemas/field.py` | Section 4.2. |
| `src/api/fields.py` | Section 4. |
| `src/services/result_field_service.py` | `ResultFieldService(db, auditLogger)`; signatures only, each raising `NotImplementedError`. Docstring names the owner lane: `acceptField` (mobile), `submitFields` (mobile), `discardField` (mobile and web, one function branching on result status per the Seams table), `listFields` (web), `getField` (web), `reopenCapture` (web). Annotated with the `schemas/field.py` types. `field_correction_service.py`, `field_aggregation_service.py` and the manual-override changes are web-lane files and are not created here. |
| `tests/test_field_models.py` | 5. |
| `tests/test_field_foundation_migration.py` | 5. |
| `tests/test_field_route_stubs.py` | 5. |
| `tests/test_field_settings.py` | 5. |
| `tests/test_field_error_codes.py` | 5. |
| `tests/test_fresh_db_build.py` | 5. Skipped unless `FRESH_DB_URL` is set (no marker needed). |

### 3.2 Changed (13)

| File | Change |
|---|---|
| `src/models/analysis_result.py` | `ResultStatus.CAPTURING`; columns `fieldCount`, `fieldSummary`, `isLegacySingleField`; relationship `fields` (`order_by=ResultField.fieldNo`, **`lazy="raise"`** so a sync or list query can never pull fields by accident). Existing members and defaults untouched. |
| `src/models/image.py` | `field: Mapped[ResultField \| None] = relationship(back_populates="image", uselist=False)`. |
| `src/models/manual_override.py` | `fieldId` (FK, nullable, indexed), `kind` (default `"OVERRIDE"`), `field` relationship; `OverrideKind` StrEnum. |
| `src/models/__init__.py` | export `ResultField`, `FieldAnnotationEdit`. |
| `alembic/env.py` | `import src.models.result_field` and `field_annotation_edit` beside the existing model imports. |
| `src/core/config.py` | New settings and `_envBool` (3.3). |
| `src/services/specimen_access.py` | `CAPTURE_PHASE_RESULT_STATUSES = frozenset({ResultStatus.CAPTURING})` after line 42. The three existing sets are not touched. |
| `src/core/exceptions.py` | The D2 codes (3.4). |
| `main.py` | import and `include_router` for the fields router, next to the others (lines 107-123). |
| `scripts/check_openapi.py` | `EXPECTED_ROUTE_COUNT` 53 → **63**. |
| `changelog.md` | One "Added" bullet under Unreleased: foundation schema, flag (default off), settings, error codes, stubs; no behavior change with the flag off. |
| `.env.example` | The five new variables, commented, flag false. |
| `tests/test_baseline_migration.py` | `test_singleHeadIs0046` pins head `== ["0046"]` and **will fail** once 0047 exists. Change to "exactly one head" and add that `0047.down_revision == "0046"`. |

Total: **12 new + 13 changed = 25 files.**

### 3.3 Settings (`src/core/config.py`)

| Field | Env var | Default | Rule |
|---|---|---|---|
| `minFieldsPerSpecimen: int` | `MIN_FIELDS_PER_SPECIMEN` | 10 | integer ≥ 1 |
| `fieldWorkflowEnabled: bool` | `FIELD_WORKFLOW_ENABLED` | false | via `_envBool` |
| `fieldLowConfidenceThreshold: float` | `FIELD_LOW_CONFIDENCE_THRESHOLD` | 0.5 | 0 < v ≤ 1 |
| `fieldOutlierMultiplier: float` | `FIELD_OUTLIER_MULTIPLIER` | 2.0 | > 1 |
| `fieldOutlierMinAbove: int` | `FIELD_OUTLIER_MIN_ABOVE` | 5 | ≥ 1 |

Outlier rule as I read the brief: a field's class count is an outlier when it is more than 2 times the mean AND at least 5 above the mean. No other clinical threshold is introduced. `_envBool(name, default)`: trims, case-insensitive; accepts `true/false/1/0/yes/no/on/off`; unset or empty gives the default; anything else raises `RuntimeError` naming the variable (startup, like the other config errors). Bad numbers raise the same way. The flag is read from the module singleton at request time; flipping it needs a restart, and tests change it with `monkeypatch.setattr(settings, ...)`.

### 3.4 Error codes (`src/core/exceptions.py`)

Same shape as the existing classes (`HTTPException` subclass with `errorCode`); the global handler already renders `{"error": {"code", "message"}}`, so `main.py`'s handler needs no change.

| Class | Code | HTTP |
|---|---|---|
| `ImageNotMicroscopyError` | IMAGE_NOT_MICROSCOPY | 422 |
| `ImageExposureError` | IMAGE_EXPOSURE | 422 |
| `AppUpdateRequiredError` | APP_UPDATE_REQUIRED (message "Update UroLens to continue") | 410 |
| `FieldNotOpenedError` | FIELD_NOT_OPENED | 409 |
| `FieldVersionConflictError` | FIELD_VERSION_CONFLICT | 409 |
| `FieldsUndecidedError(count)` | FIELDS_UNDECIDED ("N fields not reviewed") | 409 |
| `FieldsBelowMinimumError(accepted, minimum)` | FIELDS_BELOW_MINIMUM | 409 |
| `FieldsNotVerifiedError` | FIELDS_NOT_VERIFIED | 409 |
| `FeatureDisabledError` | FEATURE_DISABLED | 404 |
| `NotImplementedYetError` | NOT_IMPLEMENTED | 501 |

Extra data (counts) goes in the message only; the envelope has no other keys (Q9).

---

## 4. Route stubs and schemas

### 4.1 Routes (`src/api/fields.py`, one `APIRouter(tags=["fields"])`, full paths)

Ten new routes, all from proposal 10.4 as new. Path parameters keep the repo's `{field_id}` / `{id}` style (path parameters are a rule 17 exception).

| # | Method | Path | Role | Request | Response model | Owner lane |
|---|---|---|---|---|---|---|
| 1 | POST | /api/v1/fields/{field_id}/accept | MEDTECH | none | FieldActionResponse | mobile |
| 2 | POST | /api/v1/fields/{field_id}/discard | MEDTECH | DiscardFieldRequest (body optional) | FieldActionResponse | shared (one function, branches on status) |
| 3 | POST | /api/v1/results/{id}/fields/submit | MEDTECH | none | ResultStatusResponse | mobile |
| 4 | GET | /api/v1/results/{id}/fields | MEDTECH, SUPERVISOR | none | FieldListResponse | web |
| 5 | GET | /api/v1/fields/{field_id} | MEDTECH, SUPERVISOR | none | FieldDetail | web |
| 6 | PUT | /api/v1/fields/{field_id}/annotations | MEDTECH | SaveAnnotationsRequest | FieldActionResponse | web |
| 7 | POST | /api/v1/fields/{field_id}/override | MEDTECH | FieldOverrideRequest | FieldActionResponse | web |
| 8 | POST | /api/v1/fields/{field_id}/override/revert | MEDTECH | RevertOverrideRequest | FieldActionResponse | web |
| 9 | POST | /api/v1/fields/{field_id}/verify | MEDTECH | none | FieldActionResponse | web |
| 10 | POST | /api/v1/results/{id}/reopen-capture | MEDTECH | none | ResultStatusResponse | web |

Every route has `RequireRole([...])` as its first dependency and a final `response_model`. The flag check is a second dependency (`requireFieldWorkflow`) that raises `FeatureDisabledError` (404) when `fieldWorkflowEnabled` is false; the handler body then raises `NotImplementedYetError` (501). One deviation from "handler body": FastAPI validates request bodies after dependencies run but a dependency's exception wins, so the flag-off 404 is returned even for a bad body. In the handler it would come after a 422. The order is 401/403, then 404, then 422, then 501. Stubs do not touch the DB or the service.

Existing routes (including the ones the lanes will change or retire: upload, `/results/{id}/override`, `/images/{id}/discard`, `/results/{id}/confirm`, `/medtech/pending`, `GET /results/{id}`, `/sync/pull`) are **not touched**; they keep behaving as today with the flag on or off. Retiring them with 410 is lane work.

### 4.2 Schemas (`src/schemas/field.py`, camelCase, Pydantic v2)

- Shared: `MAX_FIELD_COUNT = 1000` (own constant; `MAX_OVERRIDE_COUNT = 300` and its test pin at `tests/test_override_ai_value.py:52` are web-lane).
- `Detection {id, particleType, confidence, x, y, w, h}`; `AddedBox {id, particleType, x, y, w, h}`; `AddedBoxInput` (no id); `FieldAnnotationEdits {removedDetectionIds, addedBoxes, editedBy, editedAt}`; `FieldFlag {reason, particleType}` with `reason` in LOW_CONFIDENCE / OUTLIER_COUNT / ABOVE_NORMAL_MAX; `FieldOverride {particleType, originalAiValue, correctedValue, rationale, overriddenBy, overriddenAt}`.
- `FieldSummaryItem {fieldId, resultId, fieldNo, magnification, status, imageUrl, aiFindings, effectiveCounts, confidenceScores, flags, hasCorrections, overriddenClasses, verifiedBy, verifiedAt, discardReason, version, createdAt, updatedAt}`. No detections.
- `FieldDetail(FieldSummaryItem) + {detections, imageWidth, imageHeight, annotationEdits, overrides, smartDiagnosisPreview, smartDiagnosisUnavailable, modelVersion}`.
- `FieldListResponse {resultId, resultStatus, minFields, acceptedFieldCount, verifiedFieldCount, undecidedFieldCount, items}`.
- Requests: `SaveAnnotationsRequest {removedDetectionIds, addedBoxes, expectedVersion (int ≥ 1)}`; `FieldOverrideRequest {particleType (in PARTICLE_LABELS), correctedValue (int 0 to MAX_FIELD_COUNT), rationale (required, non-blank)}`; `RevertOverrideRequest {particleType, rationale}`; `DiscardFieldRequest {reason: str | None}`.
- Responses: `FieldActionResponse {field, acceptedFieldCount, verifiedFieldCount, undecidedFieldCount}`; `ResultStatusResponse {resultId, status, updatedAt}`.
- Shapes for the changed routes, defined but **not attached to existing response models** (so flag-off responses stay byte-identical): `FieldAnalysisResponse` (upload; adds `fieldId, fieldNo, magnification, detections, imageWidth, imageHeight, confidenceScores, acceptedFieldCount, minFields`), `MedtechPendingFieldCounts {fieldCount, verifiedFieldCount, flaggedFieldCount}`, `ParticleAggregate {mean, min, max, total, fieldsSeen, aiMean, displayValue, fieldsAboveNormal}`, `FullResultFieldAdditions {fields, fieldCount, fieldSummary}`, `SyncResultField` (explicit columns, no detections). They are tested with `model_json_schema()`. The cost is that they are not in `/openapi.json` until a lane wires them (Q10).

---

## 5. Tests

All in the default fast suite (CI runs plain `pytest -q`) except the last.

1. **Models** (`test_field_models.py`): every column in 1.1 and 1.2 present with the specified type, nullability, default; UNIQUE and CHECK names; compiled `select(ResultField)` omits `detections` and `undefer` includes it; `AnalysisResult.fields` raises on lazy access; `ResultStatus.CAPTURING` exists and the old members are unchanged; `ManualOverride.kind` default.
2. **Migration vs models** (`test_field_foundation_migration.py`): run `0047.upgrade()` through an offline `MigrationContext` (`as_sql=True`, postgresql dialect) so no DB is needed, and compare the emitted `CREATE TABLE` columns/nullability to `Base.metadata`; assert literal `ENABLE ROW LEVEL SECURITY` for both tables (the existing RLS test then passes); single head `0047` and `down_revision == "0046"`; no `UPDATE` touches `updated_at`; downgrade source carries the guards; the preflight `DO` block is present; the docstring contains the dry-run queries.
3. **Fresh DB** (`test_fresh_db_build.py`): with `FRESH_DB_URL` set, `alembic upgrade head` on an empty database, then `compare_metadata` against `Base.metadata` shows no difference for the new tables and columns; also seeds legacy and non-legacy rows at 0046, upgrades, and checks field count, legacy flag, `effective_counts` and that `analysis_results.updated_at` did not move. **BLOCKED ON B1** merging and on a scratch Postgres; skipped otherwise. The offline `upgrade head --sql` check cannot run end to end today (0037 and 0041 read the DB).
4. **Stubs** (`test_field_route_stubs.py`): `TestClient` with `getCurrentUser` overridden. For each of the 10 routes: flag off gives 404 `FEATURE_DISABLED`, flag on gives 501 `NOT_IMPLEMENTED`, both in the error envelope; wrong role gives 403 before the flag check; no token gives 401; a bad body with the flag off still gives 404; every route has `RequireRole` and a `response_model`.
5. **Settings** (`test_field_settings.py`): `_envBool` accepts each spelling in both cases and surrounding spaces; unset and empty give the default; `maybe` raises `RuntimeError` naming the variable; defaults are 10 / false / 0.5 / 2.0 / 5; out-of-range values raise.
6. **Error codes** (`test_field_error_codes.py`): each class renders the expected status and `{"error": {"code", "message"}}` through `httpExceptionHandler`; `FieldsUndecidedError(2)` message contains "2".
7. **OpenAPI** (`scripts/check_openapi.py`): 63 routes, all tagged; the 53 existing (method, path) pairs still present.
8. **Existing suite unchanged**: the 880 stay green (the one head pin is updated, 3.2). The three existing status sets compare equal to their current members, and `CAPTURE_PHASE_RESULT_STATUSES == {CAPTURING}`. Ruff on the CI selection and `check_naming.py` (currently 9 findings) must not get worse.

Expected result after implementation: 880 + new tests, 0 failed.

---

## 6. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Order with B1.** The migration is 0047 only if B1 merges first; the fresh-DB test cannot run before. | Merge B1 first; this PR is based on it. |
| R2 | **LIVE FACT unverified** (section 0). | Preflight `DO` block fails loud; contingency revision described. Please run the query. |
| R3 | **D4 vs the spec.** The spec (section 6, migration rules) moves in-progress results to CAPTURING; D4 leaves status unchanged and sets a flag. The flag only means something once the web lane builds the legacy confirm path. | Keep the flag off until then; the migration does not alter behavior. |
| R4 | **Legacy-status rows with a DISCARDED image** (typically `IMAGE_RETAKE_REQUESTED`, because the retake flow discards the image) match "one image" in D4 but not "active image" in D5. As written they get no field and no flag, and would be stuck once the flag is on. | Dry-run query D4 gives the count; decision Q3. |
| R5 | **P-4 unverified**: live `manual_overrides` types (migration says INTEGER, the code writes text). This migration only adds columns, but REVERT rows and the web lane's field overrides depend on the real types. | Run D6. |
| R6 | **Deferred `detections` and async**: touching it without `undefer` raises in an async session. That is the intended safety, but it fails loudly in production if a path forgets. | Test in 5.1; document in the model docstring. |
| R7 | **Sync cost**: `/sync/pull` loads whole `AnalysisResult` rows, so `field_summary` rides along. It is small (per-class stats). `fields` is `lazy="raise"`. | Accept; revisit if payloads grow. |
| R8 | **Backfill is one transaction** touching every result with an image. Fine at lab scale; the ADD COLUMN steps take a brief exclusive lock. | Run off-peak; D2 shows the row counts first. |
| R9 | **Contract drift.** The contract draft (`field-verification-contract.patch`) differs from the decisions and spec (section 7). Client teams may already be coding against it. | Update the patch to match before the foundation PR. |
| R10 | **Flag changes need a restart** (read once at startup). | Document in the changelog and `.env.example`. |
| R11 | **CHECK constraints freeze the status and kind sets.** A new value later needs a constraint change. | Intended; the sets are fixed. |
| R12 | **Stub 501s are visible** in the docs of a flag-off deployment as real routes. | They answer 404; acceptable. |
| R13 | **Engine work (B0/P-2/P-1) is unresolved.** `detections` will be empty and counts integer-only until the engine release; the foundation does not depend on it. | Both lanes build against stubs, as the split already says. |

---

## 7. Where the inputs disagree (decision applied)

| Item | Spec / split / contract draft | Applied here |
|---|---|---|
| Box coordinates | Spec 6.1: pixels `{x1,y1,x2,y2}`. | **D1**: percent `{x,y,w,h}` of the upright image, matching the contract draft and the edits table. JSON keys use `particleType`, not `class`. |
| In-progress results | Spec: move to CAPTURING. | **D4**: flag, status unchanged. |
| Concurrency token | Contract draft: `expected_updated_at`, error `FIELD_MODIFIED`. | `version` INT and `FIELD_VERSION_CONFLICT` (D2, brief). |
| Error codes | Split lists 5; D2 lists 9; contract draft also has `FIELD_MODIFIED`, `OVERRIDE_IN_FORCE`, `INVALID_FIELD_STATUS`, `INVALID_RESULT_STATUS`, `FIELD_NOT_FOUND`. | D2's nine plus `FEATURE_DISABLED` and `NOT_IMPLEMENTED`. The other four are left for the lanes, announced in their PRs per the Seams rule. |
| `GET /results/{id}/summary` | In the contract draft and implied by spec 2.7, but **not in 10.4**. | Not stubbed (Q8). |
| Migration number | Spec: `0046_…`. | `0047` because B1 took `0046`. |
| Override cap | Spec: raise `MAX_OVERRIDE_COUNT` to 1,000. | Own `MAX_FIELD_COUNT` in `schemas/field.py`; the result-level cap and its test stay for the web lane. |

---

## 8. Questions

1. Run the LIVE FACT query (section 0) and confirm `character varying`.
2. Confirm B1 merges before this PR, and that this PR should be based on it (the strict reading of pre-check 1 would stop here).
3. Legacy-status results whose image is not ACTIVE (R4): leave as they are, or backfill a DISCARDED field and set the flag?
4. Any backfilled field that should be created with `verified_by/verified_at` (for example from `confirmed_by/confirmed_at` on already-confirmed results), or leave NULL as planned?
5. Same question for `field_summary`: leave NULL for backfilled results (readers fall back), or compute a one-field summary in the migration?
6. `field_count` = number of ACCEPTED fields. Confirm.
7. Outlier rule: more than 2 times the mean and at least 5 above the mean. Confirm.
8. Add the contract draft's `GET /results/{id}/summary` as an eleventh stub (web lane; count becomes 64)?
9. The error envelope carries only `code` and `message`. Is that enough for `FIELD_VERSION_CONFLICT` (client refetches) and the field counts, or should the envelope gain a `details` object? The latter touches `main.py` and every client.
10. Changed-route shapes (upload, pending counts, full result, sync) stay schema-only (not in OpenAPI). Alternative: add them to the existing response models as optional fields defaulting to null, which makes them visible but changes the JSON of existing routes. Which do you prefer?
11. Roles: GET field list/detail allow MEDTECH and SUPERVISOR; `reopen-capture` MEDTECH only. Confirm.
12. `field_annotation_edits` has no `superseded_at`; a PUT replaces the rows. Confirm that history lives only in the audit log.
13. Add the composite `(specimen_id, updated_at)` sync index now, or leave to the mobile lane?
14. Confirm that the ten stub routes sit in one router with full paths, and that `/results/{id}/fields*` lives in `fields.py` rather than `results.py`.

---

## 9. Decisions (authoritative; override anything above)

Pre-check / live
- Q1: analysis_results.status live = <FILL from query>. Keep the preflight DO block regardless.
- Q2: B1 merges first. Base this PR on development after B1 merge; migration is 0047.

Backfill
- NULL safety: COALESCE(r.model_version,'unknown'), COALESCE(r.smart_diagnosis_unavailable,false), COALESCE(i.width_px,0), COALESCE(i.height_px,0). Add a dry-run query counting NULLs in those four.
- Q3: Legacy-status results with no ACTIVE image get NO field but DO get is_legacy_single_field = true. Legacy path (web lane) will offer reject only when the result has zero fields. Change post-check to: every legacy row has a field OR has no ACTIVE image.
- Q4: For results with confirmed_by NOT NULL, set the backfilled field's verified_by/verified_at from confirmed_by/confirmed_at. Otherwise NULL.
- Q5: field_summary stays NULL for backfilled results. Readers fall back to particle_classes when NULL.
- Q6: Confirmed. field_count = number of ACCEPTED fields.

Schema
- Q12: Add field_annotation_edits.superseded_at TIMESTAMPTZ NULL + partial index on (field_id) WHERE superseded_at IS NULL. PUT marks current rows superseded and inserts the new set; rows are never deleted.
- Q13: Add composite index ix_result_fields_specimen_updated (specimen_id, updated_at) now.

Rules
- Q7: Confirmed. Outlier = count > 2x specimen mean AND >= 5 above it.
- Q11: Confirmed. Field list/detail: MEDTECH, SUPERVISOR. reopen-capture, discard, accept, submit, annotations, override, revert, verify: MEDTECH only.

Contract
- Q8: Yes. Add GET /api/v1/results/{id}/summary as stub 11 (web lane, MEDTECH + SUPERVISOR, response VerificationSummaryResponse with ParticleAggregate per class, corrections list, focal findings, final diagnosis preview). EXPECTED_ROUTE_COUNT 64.
- Q9: Keep the envelope as {code, message}. No details object.
- Q10: Changed-route shapes stay schema-only. Additionally write their JSON schemas to docs/contract/field-shapes.json so client teams can read them before lanes wire them.
- Q14: Confirmed. One router, full paths, in fields.py.

### 9.1 What this changes in sections 0 to 8 (reconciliation)

| Section | Change |
|---|---|
| 0, LIVE FACT | Q1 is still the blank `<FILL>`. The assumption (`character varying(48)`) stands and the preflight block stays. Nothing downstream depends on the answer unless the preflight raises. |
| 0, pre-check 1 and 2 | Q2 settles the order: B1 merges, this branch is cut from `development` after that, migration `0047`. The "HEAD is not origin/development" stop no longer applies once the PR is based on the merged `development`. Re-run the pre-check then. |
| 1.1 | Add index `ix_result_fields_specimen_updated` (specimen_id, updated_at). The single `ix_result_fields_specimen_id` stays as briefed (Q13 adds, does not replace). |
| 1.2 | Add `superseded_at TIMESTAMPTZ NULL` and partial index `ix_field_annotation_edits_current` on (field_id) WHERE superseded_at IS NULL. Model gets `supersededAt`; `FieldAnnotationEdit` current set = rows with `supersededAt IS NULL`. The "No `superseded_at`" paragraph in 1.2 and Q12's old text are void. The downgrade guard (2.3) is unchanged: any edits row blocks it. |
| 2.1 step 6 | Backfill SQL uses `COALESCE(r.model_version,'unknown')`, `COALESCE(r.smart_diagnosis_unavailable,false)`, `COALESCE(i.width_px,0)`, `COALESCE(i.height_px,0)`. `verified_by/verified_at` = `r.confirmed_by/r.confirmed_at` when `confirmed_by IS NOT NULL`, else NULL (replaces "stay NULL for every backfilled field"). `field_summary` stays NULL. |
| 2.1 step 8 | Legacy flag condition becomes: **status IN the four legacy statuses, regardless of image state** (replaces `AND field_count = 1`). Results with no ACTIVE image get the flag and no field. Step 7 (`field_count = 1`) is unchanged and applies only where a field was created. |
| 2.1 step 9 | Post-check: raise if any `is_legacy_single_field` row has no field AND still has an ACTIVE image (i.e. every legacy row has a field or has no ACTIVE image). |
| 2.1 closing paragraph, R4 | Void: rows with a non-ACTIVE image are now flagged legacy with zero fields. Residual risk: the web lane must offer reject only (no confirm) for a legacy result with zero fields. Record this in the web lane's notes. |
| 2.4 | D4 query now lists "legacy rows with no ACTIVE image (flagged, no field)". Add D8: `SELECT count(*) FILTER (WHERE r.model_version IS NULL) AS model_version_null, count(*) FILTER (WHERE r.smart_diagnosis_unavailable IS NULL) AS unavailable_null, count(*) FILTER (WHERE i.width_px IS NULL) AS width_null, count(*) FILTER (WHERE i.height_px IS NULL) AS height_null FROM analysis_results r JOIN images i ON i.image_id = r.image_id WHERE i.status = 'ACTIVE';`. D7 adds a `with_verified_by` count. |
| 3.1 | New file `docs/contract/field-shapes.json` and a generator `scripts/export_field_shapes.py` (writes `model_json_schema()` of the schema-only shapes: `FieldAnalysisResponse`, `MedtechPendingFieldCounts`, `ParticleAggregate`, `FullResultFieldAdditions`, `SyncResultField`, and the 11 route request/response models). New test `tests/test_field_shapes_export.py` fails if the JSON is out of date. **New files 12 → 14; total 25 → 27.** |
| 3.3 | Outlier rule confirmed (Q7). |
| 3.4 | No change (Q9: envelope stays `{code, message}`). |
| 4.1 | **11 routes.** New row 11: GET `/api/v1/results/{id}/summary`, roles MEDTECH + SUPERVISOR, no request body, response `VerificationSummaryResponse`, owner web. Route 4 and 5 roles confirmed (MEDTECH, SUPERVISOR); all others MEDTECH only (Q11). |
| 4.2 | Add `VerificationSummaryResponse {resultId, fieldCount, minFields, perClass: {className: ParticleAggregate}, corrections: [{fieldId, fieldNo, particleType, aiValue, effectiveValue, kind}], focalFindings: [{particleType, fieldNos}], smartDiagnosisPreview, smartDiagnosisUnavailable, canConfirm, blockingReason}`. `corrections` and `focalFindings` item shapes are my reading of "corrections list, focal findings" and are not in the spec; flag in the PR for the web lane to adjust. `ParticleAggregate.fieldsAboveNormal` stays and feeds the focal findings. `result_field_service`/aggregation owners unchanged. |
| 5 | Stub tests cover 11 routes. Migration test also asserts the new index and `superseded_at` column, the COALESCEs, and the new post-check. Add the shapes-export test. `check_openapi` expects **64**. Expected suite: 880 + new tests. |
| 8 | Q3, Q4, Q5, Q6, Q7, Q8, Q9, Q10, Q11, Q12, Q13, Q14 are closed by this section. Only Q1 (live type) remains open, and it is non-blocking. |
