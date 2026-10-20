"""新增本机 Agent 控制和受管启动历史；不种入运行事实、不改旧表。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261014_0009"
down_revision = "20261014_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_controls",
        sa.Column("instance_id", sa.String(32), nullable=False),
        sa.Column("agent_session_id", sa.String(32), nullable=False),
        sa.Column("agent_pid", sa.Integer(), nullable=False),
        sa.Column("agent_started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("desired_state", sa.String(16), server_default="stopped", nullable=False),
        sa.Column("state", sa.String(32), server_default="stopped", nullable=False),
        sa.Column("command_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("worker_session_id", sa.String(32), nullable=True),
        sa.Column("child_pid", sa.Integer(), nullable=True),
        sa.Column("model_ready", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("active_task_id", sa.String(32), nullable=True),
        sa.Column("last_error", sa.String(256), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("shutdown_confirmed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("length(agent_session_id) = 32", name="agent_session_id_length"),
        sa.CheckConstraint("agent_pid > 0", name="agent_pid_positive"),
        sa.CheckConstraint("desired_state IN ('running', 'stopped')", name="desired_state_allowed"),
        sa.CheckConstraint("state IN ('stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed')", name="state_allowed"),
        sa.CheckConstraint("command_revision >= 0", name="command_revision_nonnegative"),
        sa.CheckConstraint("worker_session_id IS NULL OR length(worker_session_id) = 32", name="worker_session_id_length"),
        sa.CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
        sa.ForeignKeyConstraint(["instance_id"], ["service_instances.instance_id"]),
        sa.PrimaryKeyConstraint("instance_id"),
    )
    op.create_table(
        "managed_instance_sessions",
        sa.Column("worker_session_id", sa.String(32), nullable=False),
        sa.Column("instance_id", sa.String(32), nullable=False),
        sa.Column("agent_session_id", sa.String(32), nullable=False),
        sa.Column("agent_pid", sa.Integer(), nullable=False),
        sa.Column("child_pid", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("exit_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(256), nullable=True),
        sa.CheckConstraint("length(worker_session_id) = 32", name="worker_session_id_length"),
        sa.CheckConstraint("length(agent_session_id) = 32", name="agent_session_id_length"),
        sa.CheckConstraint("agent_pid > 0", name="agent_pid_positive"),
        sa.CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
        sa.CheckConstraint("state IN ('stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed')", name="state_allowed"),
        sa.ForeignKeyConstraint(["instance_id"], ["service_instances.instance_id"]),
        sa.PrimaryKeyConstraint("worker_session_id"),
    )
    op.create_index("ix_managed_instance_sessions_started", "managed_instance_sessions", ["instance_id", "started_at", "worker_session_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("0009 降级必须连接数据库核对受管进程退出，不能离线生成降级 SQL。")
    connection = op.get_bind()
    unresolved_sessions = connection.execute(sa.text(
        "SELECT COUNT(*) FROM managed_instance_sessions WHERE exit_confirmed_at IS NULL"
    )).scalar_one()
    active_agents = connection.execute(sa.text(
        "SELECT COUNT(*) FROM agent_controls WHERE shutdown_confirmed_at IS NULL"
    )).scalar_one()
    if unresolved_sessions or active_agents:
        raise RuntimeError("不能降级：受管 ASR 或 Node Agent 尚未确认退出，请先通过受管停止入口完成确认。")
    op.drop_table("managed_instance_sessions")
    op.drop_table("agent_controls")
