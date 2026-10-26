"""baseline: tables that were provisioned by hand in Supabase

Revision ID: 0000
Revises:
Create Date: 2026-10-26

B1 (docs/multifield-readiness-merged.md). `users`, `specimens`, `audit_logs`,
`result_confirmations` and `sessions` were created directly in Supabase and no
earlier revision creates them, so `alembic upgrade head` failed on an empty
database (0004 already FKs `users`). This root revision creates them.

DDL is copied from the live database (docs/live-schema.json,
docs/live-schema-enum.json), including constraint/index names and the
duplicate specimens indexes. Every statement is IF NOT EXISTS or guarded, so
this revision is a no-op against live, where the tables already exist.

Foreign keys:
  * Created here (target table is created here): specimens.received_by,
    specimens.medtech_id, result_confirmations.medtech_id, sessions.user_id.
  * NOT created here, and no later revision adds them either:
      - specimens.lab_request_id -> lab_requests  (fk_specimens_lab_request)
      - result_confirmations.result_id -> analysis_results
        (fk_confirmations_result)
    Both targets are created by later revisions (0032, 0014). A fresh database
    therefore lacks these two FKs until a follow-up revision adds them; live
    has them.

Enums: only `specimen_status` and `user_role` are created here. No earlier
revision creates either (0021 only converts users.role away from user_role).
`user_role` is used only by sessions.user_role; users.role is VARCHAR(30).

RLS is enabled on all five tables with no policies, matching 0040.
"""
from alembic import op

revision = "0000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'specimen_status') THEN
                CREATE TYPE specimen_status AS ENUM ('RECEIVED', 'LABELED', 'ASSIGNED', 'IN_QUEUE', 'PROCESSING', 'REJECTED', 'COMPLETED');
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
                CREATE TYPE user_role AS ENUM ('RECEPTIONIST', 'MEDTECH', 'SUPERVISOR', 'PHYSICIAN', 'PATIENT', 'ADMINISTRATOR');
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id uuid NOT NULL DEFAULT gen_random_uuid(),
            username varchar(80) NOT NULL,
            hashed_password text NOT NULL,
            role varchar(30) NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            failed_attempts smallint NOT NULL DEFAULT 0,
            locked_at timestamptz,
            expo_push_token text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_users PRIMARY KEY (user_id),
            CONSTRAINT uq_users_username UNIQUE (username),
            CONSTRAINT ck_users_failed_attempts CHECK (failed_attempts >= 0)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS specimens (
            specimen_id uuid NOT NULL DEFAULT gen_random_uuid(),
            lab_request_id uuid NOT NULL,
            sample_uid varchar(30),
            status specimen_status NOT NULL DEFAULT 'RECEIVED',
            visual_check_passed boolean NOT NULL DEFAULT true,
            received_by uuid NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            assigned_at timestamptz,
            completed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            medtech_id uuid,
            patient_name varchar(255),
            patient_uid varchar(100),
            test_type varchar(255),
            priority_level varchar(20),
            rejection_reason text,
            rejection_note text,
            rejected_at timestamptz,
            CONSTRAINT pk_specimens PRIMARY KEY (specimen_id),
            CONSTRAINT uq_specimens_uid UNIQUE (sample_uid),
            CONSTRAINT fk_specimens_receiver FOREIGN KEY (received_by) REFERENCES users(user_id),
            CONSTRAINT specimens_medtech_id_fkey FOREIGN KEY (medtech_id) REFERENCES users(user_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_specimens_lab_request ON specimens USING btree (lab_request_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_specimens_specimen_id ON specimens USING btree (specimen_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_specimens_status ON specimens USING btree (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_specimens_status ON specimens USING btree (status)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id uuid NOT NULL DEFAULT gen_random_uuid(),
            event_type varchar(80) NOT NULL,
            entity_type varchar(60) NOT NULL,
            entity_id uuid NOT NULL,
            user_id uuid,
            ip_address inet,
            detail_json jsonb,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_audit_logs PRIMARY KEY (log_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs USING btree (entity_id, entity_type)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_event_time "
        "ON audit_logs USING btree (event_type, occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_user_time "
        "ON audit_logs USING btree (user_id, occurred_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS result_confirmations (
            confirmation_id uuid NOT NULL DEFAULT gen_random_uuid(),
            result_id uuid NOT NULL,
            medtech_id uuid NOT NULL,
            confirmed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_result_confirmations PRIMARY KEY (confirmation_id),
            CONSTRAINT uq_confirmations_result UNIQUE (result_id),
            CONSTRAINT fk_confirmations_medtech FOREIGN KEY (medtech_id) REFERENCES users(user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_result_confirmations_result "
        "ON result_confirmations USING btree (result_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id uuid NOT NULL DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL,
            user_role user_role NOT NULL,
            ip_address inet,
            user_agent text,
            is_active boolean NOT NULL DEFAULT true,
            login_at timestamptz NOT NULL DEFAULT now(),
            logout_at timestamptz,
            last_activity_at timestamptz,
            CONSTRAINT pk_sessions PRIMARY KEY (session_id),
            CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_active "
        "ON sessions USING btree (user_id, is_active) WHERE (is_active = true)"
    )

    for table in ("users", "specimens", "audit_logs", "result_confirmations", "sessions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    raise RuntimeError("baseline revision cannot be downgraded")
