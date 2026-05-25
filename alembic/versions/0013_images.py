"""create images table

Revision ID: 0013
Revises: 0005
Create Date: 2026-05-23

Source: Migration 0013 — T2.7 Image Retake / Re-upload
Depends on: specimens, users
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # Create image_status ENUM if it doesn't exist
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'image_status') THEN
                CREATE TYPE image_status AS ENUM ('ACTIVE', 'DISCARDED', 'REPLACED');
            END IF;
        END$$;
    """)

    op.create_table(
        "images",
        sa.Column(
            "image_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "specimen_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("specimens.specimen_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("file_format", sa.String(10), nullable=False),
        sa.Column("width_px", sa.SmallInteger(), nullable=False),
        sa.Column("height_px", sa.SmallInteger(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "uploaded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("discarded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("file_format IN ('JPEG', 'PNG')", name="ck_images_format"),
        sa.CheckConstraint(
            "width_px >= 640 AND height_px >= 480", name="ck_images_resolution"
        ),
    )

    op.create_index("idx_images_specimen", "images", ["specimen_id"])
    op.create_index("idx_images_status", "images", ["status"])


def downgrade():
    op.drop_index("idx_images_status", table_name="images")
    op.drop_index("idx_images_specimen", table_name="images")
    op.drop_table("images")
    op.execute("DROP TYPE IF EXISTS image_status;")
