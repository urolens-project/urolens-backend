# Migrations: fresh builds, guards and known limits

## Deploy order after live's 0048

`0049` result_returns field pointer, `0050` result_fields capture id, `0051`
specimens.patient_name to TEXT, `0052` created_at renamed to live's names (no-op on
live), `0053` analysis_results.smart_diagnosis (no-op on live).

## Existence guards added to old migrations

An empty database now builds to head. The old migrations were edited only to add these
guards; where the precondition holds, behaviour is unchanged.

| # | Revision | Change | Precondition | Why it was missing on a fresh build |
|---|---|---|---|---|
| 1 | 0021 | `depends_on = "0011"` | n/a | 0021 alters `notifications`, which 0011 creates on the other branch. |
| 2 | 0021 `upgrade()` | rename `type` to `notification_type` | `notifications.type` exists | 0011 already creates `notification_type`. |
| 3 | 0021 `downgrade()` | rename back and retype to the enum | the `notification_type` enum exists | a fresh build never creates that enum. |
| 4 | 0047 `upgrade()` | step 6 backfill `INSERT…SELECT` | the seven `analysis_results` columns it reads exist | live got `smart_diagnosis` outside the migrations; a fresh build has no rows. Offline (`--sql`) it renders unguarded. |

## No-op downgrades

`0052` and `0053` downgrade to nothing. Live already has what they create, so
reversing them would break live, and after the fact a migration cannot tell the two
databases apart. `0051` does downgrade, but refuses while a `patient_name` is over 255
characters.

## `downgrade base` is not supported

The 0020 downgrade runs `ALTER COLUMN status TYPE result_status` on a column with a
VARCHAR default, and Postgres refuses the cast. Downgrades work from head down to 0020
(tested); rebuild from empty instead of going to base.

## Squashed baseline

The fresh schema still differs from live in constraint and index names, some column
types and a few unmapped live-only columns (and `qc_reviews`). A squashed baseline that
matches live is planned after this deploy.

## Checking

- Fresh build: `FRESH_DB_URL=<empty scratch db> pytest tests/test_fresh_db_build.py`
- Live's schema: `LIVE_COPY_URL=<copy> pytest tests/db` (see `tests/db/conftest.py`)
