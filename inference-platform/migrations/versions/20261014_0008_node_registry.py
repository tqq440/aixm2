"""新增本机节点、ASR 实例和实际 Worker 会话观测；不创建虚构运行记录。"""

from alembic import op
import sqlalchemy as sa


revision = "20261014_0008"
down_revision = "20261013_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compute_nodes",
        sa.Column("node_id", sa.String(32), nullable=False),
        sa.Column("local_identity", sa.String(64), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("os_name", sa.String(64), nullable=False),
        sa.Column("os_version", sa.String(255), nullable=False),
        sa.Column("architecture", sa.String(64), nullable=False),
        sa.Column("cpu_count", sa.Integer(), nullable=True),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("length(node_id) = 32", name="node_id_length"),
        sa.CheckConstraint("length(local_identity) = 64", name="local_identity_length"),
        sa.CheckConstraint("cpu_count IS NULL OR cpu_count > 0", name="cpu_count_positive"),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint("local_identity"),
    )
    op.create_index("ix_compute_nodes_registered", "compute_nodes", ["registered_at", "node_id"])
    op.create_table(
        "service_instances",
        sa.Column("instance_id", sa.String(32), nullable=False),
        sa.Column("node_id", sa.String(32), nullable=False),
        sa.Column("instance_key", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("adapter_key", sa.String(64), nullable=False),
        sa.Column("execution_device", sa.String(16), nullable=False),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("length(instance_id) = 32", name="instance_id_length"),
        sa.CheckConstraint("instance_key = 'asr-default'", name="instance_key_allowed"),
        sa.CheckConstraint("execution_device = 'cpu'", name="execution_device_allowed"),
        sa.ForeignKeyConstraint(["node_id"], ["compute_nodes.node_id"]),
        sa.ForeignKeyConstraint(["model_id", "model_version"], ["model_versions.model_id", "model_versions.model_version"]),
        sa.PrimaryKeyConstraint("instance_id"),
        sa.UniqueConstraint("node_id", "instance_key"),
    )
    op.create_index("ix_service_instances_registered", "service_instances", ["registered_at", "instance_id"])
    op.create_table(
        "instance_sessions",
        sa.Column("session_id", sa.String(32), nullable=False),
        sa.Column("instance_id", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("worker_session_id", sa.String(32), nullable=False),
        sa.Column("worker_pid", sa.Integer(), nullable=False),
        sa.Column("child_pid", sa.Integer(), nullable=True),
        sa.Column("reported_state", sa.String(32), nullable=False),
        sa.Column("reported_model_ready", sa.Boolean(), nullable=False),
        sa.Column("active_task_id", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("worker_started_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(256), nullable=True),
        sa.CheckConstraint("length(session_id) = 32", name="session_id_length"),
        sa.CheckConstraint("length(worker_session_id) = 32", name="worker_session_id_length"),
        sa.CheckConstraint("source = 'legacy_worker_observation'", name="source_allowed"),
        sa.CheckConstraint("worker_pid > 0", name="worker_pid_positive"),
        sa.CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
        sa.ForeignKeyConstraint(["instance_id"], ["service_instances.instance_id"]),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("instance_id", "worker_session_id"),
    )
    op.create_index("ix_instance_sessions_instance_started", "instance_sessions", ["instance_id", "worker_started_at", "session_id"])


def downgrade() -> None:
    op.drop_table("instance_sessions")
    op.drop_table("service_instances")
    op.drop_table("compute_nodes")
