"""公共调用/多资源预约/任务事件；不回填旧任务或许可。"""

from alembic import context, op
import sqlalchemy as sa

revision = "20261021_0016"
down_revision = "20261020_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inference_application_budgets",
        sa.Column("application_id", sa.String(64), primary_key=True),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allowed_services", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("concurrency_limit BETWEEN 1 AND 64", name="limit_range"),
        sa.CheckConstraint("version >= 1", name="positive_version"))
    op.create_table("inference_invocations",
        sa.Column("invocation_id", sa.String(32), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("tasks.task_id"), nullable=True),
        sa.Column("online_request_id", sa.String(32), sa.ForeignKey("online_requests.online_request_id"), nullable=True),
        sa.Column("application_id", sa.String(64), sa.ForeignKey("inference_application_budgets.application_id"), nullable=False),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("inference_services.service_id"), nullable=False),
        sa.Column("service_revision", sa.String(64), sa.ForeignKey("inference_service_revisions.revision"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("queue_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False), sa.Column("permit_state", sa.String(16), nullable=False),
        sa.Column("selected_target_id", sa.String(64), nullable=True), sa.Column("selected_target_key", sa.String(64), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True), sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True), sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True), sa.Column("resolved_outcome", sa.String(32), nullable=True),
        sa.Column("last_reason", sa.String(64), nullable=True), sa.Column("result_fingerprint", sa.String(64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=False), sa.Column("queue_deadline_at", sa.DateTime(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True), sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("(task_id IS NOT NULL AND online_request_id IS NULL) OR (task_id IS NULL AND online_request_id IS NOT NULL)", name="one_owner"),
        sa.CheckConstraint("state IN ('waiting','reserved','sent','finished')", name="state_allowed"),
        sa.CheckConstraint("permit_state IN ('waiting','held','uncertain','released')", name="permit_allowed"),
        sa.CheckConstraint("kind IN ('asr','vision','llm')", name="kind_allowed"),
        sa.CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
        sa.UniqueConstraint("task_id"), sa.UniqueConstraint("online_request_id"))
    op.create_index("ix_inference_invocations_waiting", "inference_invocations", ["state", "queue_key", "accepted_at"])
    op.create_index("ix_inference_invocations_expiry", "inference_invocations", ["state", "lease_expires_at", "deadline_at"])
    op.create_table("inference_reservation_items",
        sa.Column("invocation_id", sa.String(32), sa.ForeignKey("inference_invocations.invocation_id"), primary_key=True),
        sa.Column("resource_key", sa.String(160), primary_key=True), sa.Column("limit_snapshot", sa.Integer(), nullable=False),
        sa.CheckConstraint("limit_snapshot > 0", name="positive_limit"))
    op.create_index("ix_inference_reservation_items_resource", "inference_reservation_items", ["resource_key"])
    op.create_table("inference_task_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(32), nullable=False, unique=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("tasks.task_id"), nullable=True),
        sa.Column("online_request_id", sa.String(32), sa.ForeignKey("online_requests.online_request_id"), nullable=True),
        sa.Column("invocation_id", sa.String(32), sa.ForeignKey("inference_invocations.invocation_id"), nullable=True),
        sa.Column("application_id", sa.String(64), nullable=False), sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False), sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("(task_id IS NOT NULL AND online_request_id IS NULL) OR (task_id IS NULL AND online_request_id IS NOT NULL)", name="one_owner"))
    op.create_index("ix_inference_task_events_task_sequence", "inference_task_events", ["task_id", "sequence"])
    op.create_index("ix_inference_task_events_online_sequence", "inference_task_events", ["online_request_id", "sequence"])
    op.create_table("inference_scheduler_cursors", sa.Column("cursor_id", sa.String(32), primary_key=True),
                    sa.Column("last_queue_key", sa.String(128), nullable=True))
    op.create_table("inference_cpu_profiles",
        sa.Column("service_revision", sa.String(64), sa.ForeignKey("inference_service_revisions.revision"), primary_key=True),
        sa.Column("target_id", sa.String(64), primary_key=True), sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column("cpu_budget_seconds", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("公共执行降级须在线检查")
    tables = ["inference_task_events", "inference_reservation_items", "inference_cpu_profiles",
              "inference_scheduler_cursors", "inference_invocations", "inference_application_budgets"]
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {name}")) for name in tables):
        raise RuntimeError("公共执行已有记录，拒绝删除任务事件或容量账本")
    for name in tables:
        op.drop_table(name)
