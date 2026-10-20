"""建立任务、幂等记录和待投递事件；不建立通知队列。

Revision ID: 20261012_0002
Revises: 20261012_0001
Create Date: 2026-10-12
"""

from alembic import op
import sqlalchemy as sa

revision: str = "20261012_0002"
down_revision: str | None = "20261012_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("caller_id", sa.String(64), nullable=False),
        sa.Column("task_type", sa.String(32), server_default="asr", nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("input_ref", sa.String(32), nullable=False),
        sa.Column("input_filename", sa.String(255), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("input_size_bytes", sa.Integer(), nullable=False),
        sa.Column("audio_duration_seconds", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), server_default="created", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("execution_plan_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("dispatch_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("queue_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(task_id) = 32", name=op.f("ck_tasks_task_id_length")),
        sa.CheckConstraint("length(input_ref) = 32", name=op.f("ck_tasks_input_ref_length")),
        sa.CheckConstraint("length(input_sha256) = 64", name=op.f("ck_tasks_input_sha256_length")),
        sa.CheckConstraint("input_size_bytes > 0", name=op.f("ck_tasks_input_size_positive")),
        sa.CheckConstraint("audio_duration_seconds > 0", name=op.f("ck_tasks_audio_duration_positive")),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_tasks_attempt_nonnegative")),
        sa.CheckConstraint("execution_plan_version >= 1", name=op.f("ck_tasks_execution_plan_version_positive")),
        sa.CheckConstraint("dispatch_version >= 1", name=op.f("ck_tasks_dispatch_version_positive")),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("input_ref", name=op.f("uq_tasks_input_ref")),
    )
    op.create_index("ix_tasks_caller_created", "tasks", ["caller_id", "created_at", "task_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("caller_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(id) = 32", name=op.f("ck_idempotency_records_id_length")),
        sa.CheckConstraint("length(request_fingerprint) = 64", name=op.f("ck_idempotency_records_request_fingerprint_length")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], name=op.f("fk_idempotency_records_task_id_tasks")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint("caller_id", "idempotency_key", name="uq_idempotency_records_caller_key"),
        sa.UniqueConstraint("task_id", name=op.f("uq_idempotency_records_task_id")),
    )
    op.create_table(
        "dispatch_events",
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), server_default="task.created", nullable=False),
        sa.Column("execution_plan_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("dispatch_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("queue_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("available_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(event_id) = 32", name=op.f("ck_dispatch_events_event_id_length")),
        sa.CheckConstraint("execution_plan_version >= 1", name=op.f("ck_dispatch_events_execution_plan_version_positive")),
        sa.CheckConstraint("dispatch_version >= 1", name=op.f("ck_dispatch_events_dispatch_version_positive")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], name=op.f("fk_dispatch_events_task_id_tasks")),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_dispatch_events")),
        sa.UniqueConstraint("task_id", "dispatch_version", name="uq_dispatch_events_task_dispatch_version"),
    )
    op.create_index("ix_dispatch_events_status_available", "dispatch_events", ["status", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_dispatch_events_status_available", table_name="dispatch_events")
    op.drop_table("dispatch_events")
    op.drop_table("idempotency_records")
    op.drop_index("ix_tasks_caller_created", table_name="tasks")
    op.drop_table("tasks")
