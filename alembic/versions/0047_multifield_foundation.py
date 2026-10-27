"""multi-field foundation: result_fields, field_annotation_edits, backfill

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-26 00:00:00.000000

Shared foundation for the multi-field workflow (docs/foundation-plan.md). Purely
additive and safe with `fieldWorkflowEnabled=false`: no existing column is
altered, and nothing in the app reads the new objects yet.

Needs B1 (0000, 0046) merged first. Written to be run by hand; not executed here.

What it does, in order
  0. Preflight: refuse to run unless analysis_results.status is VARCHAR (if it is
     still the result_status enum, ship an earlier revision that runs
     `ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'CAPTURING'` inside
     `op.get_context().autocommit_block()`).
  1. result_fields.   2. field_annotation_edits.   3. RLS on both (no policies).
  4. manual_overrides: field_id, kind.   5. analysis_results: field_count,
     field_summary, is_legacy_single_field.
  6. Backfill one ACCEPTED field (field 1, no boxes) per result with an ACTIVE image.
  7. field_count = number of ACCEPTED fields.
  8. Legacy flag on results still in a single-image status; their status is not
     changed (they are NOT moved to CAPTURING).
  9. Post-check.

Backfill rules
  * Any result with an ACTIVE image gets one ACCEPTED field; effective_counts is
    particle_classes when set, else ai_findings.
  * Results at PENDING_CONFIRM, IMAGE_RETAKE_REQUESTED, FAILED or
    RETURNED_FOR_CORRECTION also get is_legacy_single_field = true, whether or
    not they have an ACTIVE image. One with no ACTIVE image gets no field; the
    web lane may only reject it.
  * verified_by/verified_at come from confirmed_by/confirmed_at when confirmed_by
    is set, else NULL. field_summary stays NULL (readers fall back to
    particle_classes).
  * analysis_results.updated_at is deliberately not touched, so /sync/pull does
    not resend every result. The new field rows get updated_at = now(), so a
    client with an older cursor receives them once the flag is on.

Dry-run SELECTs: run these on live BEFORE upgrading.

  -- D1 live type of analysis_results.status (expect character varying / varchar / 48)
  SELECT data_type, udt_name, character_maximum_length FROM information_schema.columns
   WHERE table_name = 'analysis_results' AND column_name = 'status';

  -- D2 what the backfill will touch: status x image status
  SELECT r.status, i.status AS image_status, count(*) FROM analysis_results r
    LEFT JOIN images i ON i.image_id = r.image_id GROUP BY 1, 2 ORDER BY 1, 2;

  -- D3 rows that get a field AND the legacy flag
  SELECT r.result_id, r.status FROM analysis_results r JOIN images i ON i.image_id = r.image_id
   WHERE i.status = 'ACTIVE'
     AND r.status IN ('PENDING_CONFIRM', 'IMAGE_RETAKE_REQUESTED', 'FAILED', 'RETURNED_FOR_CORRECTION');

  -- D4 legacy-status rows with no ACTIVE image: flagged, but get no field
  SELECT r.result_id, r.status, i.status AS image_status FROM analysis_results r
    LEFT JOIN images i ON i.image_id = r.image_id
   WHERE r.status IN ('PENDING_CONFIRM', 'IMAGE_RETAKE_REQUESTED', 'FAILED', 'RETURNED_FOR_CORRECTION')
     AND (i.image_id IS NULL OR i.status <> 'ACTIVE');

  -- D5 one image shared by several results would break UNIQUE(image_id); expect 0 rows
  SELECT image_id, count(*) FROM analysis_results WHERE image_id IS NOT NULL
   GROUP BY 1 HAVING count(*) > 1;

  -- D6 manual_overrides live column types (expect text / text / varchar(60))
  SELECT column_name, data_type, character_maximum_length, is_nullable
    FROM information_schema.columns WHERE table_name = 'manual_overrides' ORDER BY ordinal_position;

  -- D7 after upgrade: fields vs results with an active image, legacy rows, confirmed rows
  SELECT (SELECT count(*) FROM result_fields) AS fields,
         (SELECT count(*) FROM analysis_results WHERE is_legacy_single_field) AS legacy,
         (SELECT count(*) FROM analysis_results WHERE field_count = 1) AS with_one_field,
         (SELECT count(*) FROM result_fields WHERE verified_by IS NOT NULL) AS with_verified_by;

  -- D8 NULLs the backfill COALESCEs away (model_version -> 'unknown',
  --    smart_diagnosis_unavailable -> false, width/height -> 0)
  SELECT count(*) FILTER (WHERE r.model_version IS NULL) AS model_version_null,
         count(*) FILTER (WHERE r.smart_diagnosis_unavailable IS NULL) AS unavailable_null,
         count(*) FILTER (WHERE i.width_px IS NULL) AS width_null,
         count(*) FILTER (WHERE i.height_px IS NULL) AS height_null
    FROM analysis_results r JOIN images i ON i.image_id = r.image_id WHERE i.status = 'ACTIVE';

Downgrade is for a scratch or staging database only. It refuses if any
field_annotation_edits row exists, any manual_overrides.field_id is set, or any
result is CAPTURING, since that data cannot be represented without these objects.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0047'
down_revision: str | Sequence[str] | None = '0046'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    """Upgrade schema."""
    # 0. Preflight
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'analysis_results' AND column_name = 'status'
                   AND data_type = 'character varying'
            ) THEN
                RAISE EXCEPTION 'analysis_results.status is not VARCHAR; see the 0047 docstring';
            END IF;
        END
        $$;
        """
    )

    # 1. result_fields
    op.create_table(
        'result_fields',
        sa.Column('field_id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('result_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('specimen_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('image_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('field_no', sa.SmallInteger(), nullable=False),
        sa.Column('magnification', sa.String(3), nullable=False, server_default='HPF'),
        sa.Column('status', sa.String(20), nullable=False, server_default='ANALYZED'),
        _jsonb('ai_findings', "'{}'::jsonb"),
        _jsonb('detections', "'[]'::jsonb"),
        sa.Column('image_width', sa.Integer(), nullable=False),
        sa.Column('image_height', sa.Integer(), nullable=False),
        _jsonb('confidence_scores', "'{}'::jsonb"),
        _jsonb('effective_counts', "'{}'::jsonb"),
        sa.Column('smart_diagnosis', postgresql.JSONB(), nullable=True),
        sa.Column('smart_diagnosis_unavailable', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('model_version', sa.String(30), nullable=False),
        sa.Column('discard_reason', sa.Text(), nullable=True),
        sa.Column('verified_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('verified_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('last_opened_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('last_opened_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('last_corrected_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('field_id', name='pk_result_fields'),
        sa.ForeignKeyConstraint(['result_id'], ['analysis_results.result_id'],
                                name='fk_result_fields_result', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['specimen_id'], ['specimens.specimen_id'],
                                name='fk_result_fields_specimen', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['image_id'], ['images.image_id'],
                                name='fk_result_fields_image', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['verified_by'], ['users.user_id'],
                                name='fk_result_fields_verified_by', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['last_opened_by'], ['users.user_id'],
                                name='fk_result_fields_opened_by', ondelete='RESTRICT'),
        sa.UniqueConstraint('image_id', name='uq_result_fields_image'),
        sa.UniqueConstraint('result_id', 'field_no', name='uq_result_fields_result_field_no'),
        sa.CheckConstraint("magnification IN ('HPF', 'LPF')", name='ck_result_fields_magnification'),
        sa.CheckConstraint("status IN ('ANALYZED', 'ACCEPTED', 'DISCARDED')",
                           name='ck_result_fields_status'),
        sa.CheckConstraint('version >= 1', name='ck_result_fields_version'),
    )
    op.create_index('ix_result_fields_result_id', 'result_fields', ['result_id'])
    op.create_index('ix_result_fields_specimen_id', 'result_fields', ['specimen_id'])
    op.create_index('ix_result_fields_specimen_updated', 'result_fields',
                    ['specimen_id', 'updated_at'])

    # 2. field_annotation_edits
    op.create_table(
        'field_annotation_edits',
        sa.Column('edit_id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('field_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('detection_id', sa.String(64), nullable=True),
        sa.Column('particle_type', sa.String(60), nullable=False),
        sa.Column('x', sa.REAL(), nullable=True),
        sa.Column('y', sa.REAL(), nullable=True),
        sa.Column('w', sa.REAL(), nullable=True),
        sa.Column('h', sa.REAL(), nullable=True),
        sa.Column('edited_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('edited_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('superseded_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('edit_id', name='pk_field_annotation_edits'),
        sa.ForeignKeyConstraint(['field_id'], ['result_fields.field_id'],
                                name='fk_field_annotation_edits_field', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['edited_by'], ['users.user_id'],
                                name='fk_field_annotation_edits_user', ondelete='RESTRICT'),
        sa.CheckConstraint("action IN ('REMOVED', 'ADDED')", name='ck_field_annotation_edits_action'),
        sa.CheckConstraint(
            "(action = 'REMOVED' AND detection_id IS NOT NULL) OR "
            "(action = 'ADDED' AND x IS NOT NULL AND y IS NOT NULL "
            "AND w IS NOT NULL AND h IS NOT NULL)",
            name='ck_field_annotation_edits_shape',
        ),
    )
    op.create_index('ix_field_annotation_edits_field_id', 'field_annotation_edits', ['field_id'])
    op.create_index('ix_field_annotation_edits_current', 'field_annotation_edits', ['field_id'],
                    postgresql_where=sa.text('superseded_at IS NULL'))

    # 3. RLS (no policies), as 0040
    op.execute("ALTER TABLE result_fields ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE field_annotation_edits ENABLE ROW LEVEL SECURITY")

    # 4. manual_overrides
    op.execute(
        "ALTER TABLE manual_overrides ADD COLUMN IF NOT EXISTS field_id UUID "
        "CONSTRAINT fk_manual_overrides_field REFERENCES result_fields(field_id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE manual_overrides ADD COLUMN IF NOT EXISTS kind VARCHAR(10) "
        "NOT NULL DEFAULT 'OVERRIDE'"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_manual_overrides_field_id ON manual_overrides (field_id)")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_manual_overrides_kind') THEN
                ALTER TABLE manual_overrides ADD CONSTRAINT ck_manual_overrides_kind
                    CHECK (kind IN ('OVERRIDE', 'REVERT'));
            END IF;
        END
        $$;
        """
    )

    # 5. analysis_results
    op.execute("ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS field_count SMALLINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS field_summary JSONB")
    op.execute(
        "ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS is_legacy_single_field "
        "BOOLEAN NOT NULL DEFAULT false"
    )

    # 6. Backfill: one ACCEPTED field per result with an ACTIVE image
    op.execute(
        """
        INSERT INTO result_fields (
            field_id, result_id, specimen_id, image_id, field_no, magnification, status,
            ai_findings, detections, image_width, image_height, confidence_scores,
            effective_counts, smart_diagnosis, smart_diagnosis_unavailable, model_version,
            verified_by, verified_at, created_at, updated_at
        )
        SELECT gen_random_uuid(), r.result_id, r.specimen_id, i.image_id, 1, 'HPF', 'ACCEPTED',
               COALESCE(r.ai_findings, '{}'::jsonb), '[]'::jsonb,
               COALESCE(i.width_px, 0), COALESCE(i.height_px, 0), '{}'::jsonb,
               CASE WHEN r.particle_classes IS NOT NULL AND r.particle_classes <> '{}'::jsonb
                    THEN r.particle_classes ELSE COALESCE(r.ai_findings, '{}'::jsonb) END,
               r.smart_diagnosis, COALESCE(r.smart_diagnosis_unavailable, false),
               COALESCE(r.model_version, 'unknown'),
               r.confirmed_by,
               CASE WHEN r.confirmed_by IS NOT NULL THEN r.confirmed_at END,
               i.uploaded_at, now()
          FROM analysis_results r
          JOIN images i ON i.image_id = r.image_id
         WHERE i.status = 'ACTIVE'
           AND NOT EXISTS (SELECT 1 FROM result_fields f WHERE f.image_id = i.image_id)
        """
    )

    # 7. field_count = number of ACCEPTED fields (updated_at deliberately untouched)
    op.execute(
        """
        UPDATE analysis_results r
           SET field_count = s.n
          FROM (SELECT result_id, count(*) AS n FROM result_fields
                 WHERE status = 'ACCEPTED' GROUP BY result_id) s
         WHERE s.result_id = r.result_id
        """
    )

    # 8. Legacy flag: status unchanged, not moved to CAPTURING
    op.execute(
        """
        UPDATE analysis_results
           SET is_legacy_single_field = true
         WHERE status IN ('PENDING_CONFIRM', 'IMAGE_RETAKE_REQUESTED', 'FAILED',
                          'RETURNED_FOR_CORRECTION')
        """
    )

    # 9. Post-check: every legacy row has a field or has no ACTIVE image
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM analysis_results r
                  JOIN images i ON i.image_id = r.image_id AND i.status = 'ACTIVE'
                 WHERE r.is_legacy_single_field
                   AND NOT EXISTS (SELECT 1 FROM result_fields f WHERE f.result_id = r.result_id)
            ) THEN
                RAISE EXCEPTION '0047 backfill left a legacy result with an ACTIVE image and no field';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema. Scratch or staging databases only; see the docstring."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM field_annotation_edits)
               OR EXISTS (SELECT 1 FROM manual_overrides WHERE field_id IS NOT NULL)
               OR EXISTS (SELECT 1 FROM analysis_results WHERE status = 'CAPTURING') THEN
                RAISE EXCEPTION '0047 cannot be downgraded: multi-field data exists';
            END IF;
        END
        $$;
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_manual_overrides_field_id")
    op.execute("ALTER TABLE manual_overrides DROP CONSTRAINT IF EXISTS ck_manual_overrides_kind")
    op.execute("ALTER TABLE manual_overrides DROP CONSTRAINT IF EXISTS fk_manual_overrides_field")
    op.execute("ALTER TABLE manual_overrides DROP COLUMN IF EXISTS kind")
    op.execute("ALTER TABLE manual_overrides DROP COLUMN IF EXISTS field_id")
    op.execute("ALTER TABLE analysis_results DROP COLUMN IF EXISTS is_legacy_single_field")
    op.execute("ALTER TABLE analysis_results DROP COLUMN IF EXISTS field_summary")
    op.execute("ALTER TABLE analysis_results DROP COLUMN IF EXISTS field_count")
    op.drop_table('field_annotation_edits')
    op.drop_table('result_fields')
