"""外部调用目标、共享容量及许可；只新增结构，不改旧调用事实。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261019_0011"
down_revision = "20261015_0010"
branch_labels = None
depends_on = None

TABLES = (
    "asr_capacity_groups", "asr_compute_targets", "asr_target_revisions",
    "asr_target_worker_sessions", "asr_call_permits",
)


def upgrade():
    op.create_table("asr_capacity_groups",
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("capacity_limit", sa.Integer(), server_default="1", nullable=False),
        sa.Column("independence_evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(group_id) BETWEEN 1 AND 64", name="group_id_length"),
        sa.CheckConstraint("capacity_limit > 0", name="capacity_limit_positive"),
        sa.PrimaryKeyConstraint("group_id"))
    op.create_table("asr_compute_targets",
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("capacity_group_id", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("target_limit", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(target_id) BETWEEN 1 AND 64", name="target_id_length"),
        sa.CheckConstraint("target_limit > 0", name="target_limit_positive"),
        sa.ForeignKeyConstraint(["capacity_group_id"], ["asr_capacity_groups.group_id"]),
        sa.PrimaryKeyConstraint("target_id"))
    op.create_index("ix_asr_compute_targets_group", "asr_compute_targets", ["capacity_group_id", "target_id"])
    op.create_table("asr_target_revisions",
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("endpoint_identity", sa.String(64), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("device_kind", sa.String(16), nullable=False),
        sa.Column("device_evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(revision_id) BETWEEN 1 AND 64", name="revision_id_length"),
        sa.CheckConstraint("length(config_fingerprint) = 64", name="config_fingerprint_length"),
        sa.CheckConstraint("length(endpoint_identity) = 64", name="endpoint_identity_length"),
        sa.CheckConstraint("device_kind IN ('unknown', 'cpu', 'gpu')", name="device_kind_allowed"),
        sa.ForeignKeyConstraint(["target_id"], ["asr_compute_targets.target_id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint("target_id", "config_fingerprint"))
    op.create_index("ix_asr_target_revisions_endpoint", "asr_target_revisions", ["endpoint_identity", "target_id"])
    op.create_table("asr_target_worker_sessions",
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("worker_key", sa.String(128), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("active_slot_key", sa.String(128), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("model_ready", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("length(session_id) = 32", name="session_id_length"),
        sa.CheckConstraint("length(worker_key) BETWEEN 1 AND 128", name="worker_key_length"),
        sa.CheckConstraint("slot_index >= 0", name="slot_index_nonnegative"),
        sa.CheckConstraint("active_slot_key IS NULL OR length(active_slot_key) BETWEEN 1 AND 128", name="active_slot_key_length"),
        sa.CheckConstraint("pid > 0", name="pid_positive"),
        sa.CheckConstraint("length(state) BETWEEN 1 AND 32", name="state_length"),
        sa.ForeignKeyConstraint(["revision_id"], ["asr_target_revisions.revision_id"]),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("active_slot_key"))
    op.create_index("ix_asr_target_worker_sessions_revision", "asr_target_worker_sessions", ["revision_id", "started_at", "session_id"])
    op.create_table("asr_call_permits",
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("capacity_group_id", sa.String(64), nullable=False),
        sa.Column("worker_session_id", sa.String(32), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("length(task_id) = 32", name="task_id_length"),
        sa.CheckConstraint("length(lease_token) = 32", name="lease_token_length"),
        sa.CheckConstraint("state IN ('held', 'uncertain', 'released')", name="state_allowed"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["asr_target_revisions.revision_id"]),
        sa.ForeignKeyConstraint(["capacity_group_id"], ["asr_capacity_groups.group_id"]),
        sa.ForeignKeyConstraint(["worker_session_id"], ["asr_target_worker_sessions.session_id"]),
        sa.PrimaryKeyConstraint("task_id"))
    op.create_index("ix_asr_call_permits_group_state", "asr_call_permits", ["capacity_group_id", "state"])
    op.create_index("ix_asr_call_permits_revision_state", "asr_call_permits", ["revision_id", "state"])
    op.create_index("ix_asr_call_permits_worker_state", "asr_call_permits", ["worker_session_id", "state"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("ASR 目标降级须在线核对数据")
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f'SELECT count(*) FROM "{name}"')) for name in TABLES):
        raise RuntimeError("已有 ASR 目标、会话或许可记录，拒绝有损降级；请保留备份并核对恢复方案")
    op.drop_index("ix_asr_call_permits_worker_state", table_name="asr_call_permits")
    op.drop_index("ix_asr_call_permits_revision_state", table_name="asr_call_permits")
    op.drop_index("ix_asr_call_permits_group_state", table_name="asr_call_permits")
    op.drop_table("asr_call_permits")
    op.drop_index("ix_asr_target_worker_sessions_revision", table_name="asr_target_worker_sessions")
    op.drop_table("asr_target_worker_sessions")
    op.drop_index("ix_asr_target_revisions_endpoint", table_name="asr_target_revisions")
    op.drop_table("asr_target_revisions")
    op.drop_index("ix_asr_compute_targets_group", table_name="asr_compute_targets")
    op.drop_table("asr_compute_targets")
    op.drop_table("asr_capacity_groups")
