"""受控多调用槽：保留旧调用事实，容量审计与槽报告增量。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261019_0012"
down_revision = "20261019_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("asr_target_worker_sessions", sa.Column("active_task_id", sa.String(32), nullable=True))
    op.add_column("asr_target_worker_sessions", sa.Column("child_pid", sa.Integer(), nullable=True))
    op.add_column("asr_target_worker_sessions", sa.Column("last_error", sa.String(256), nullable=True))
    op.create_table("asr_capacity_changes",
        sa.Column("change_id", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("capacity_group_id", sa.String(64), nullable=False),
        sa.Column("previous_target_limit", sa.Integer(), nullable=False),
        sa.Column("target_limit", sa.Integer(), nullable=False),
        sa.Column("previous_group_limit", sa.Integer(), nullable=False),
        sa.Column("group_limit", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(change_id) = 32", name="change_id_length"),
        sa.CheckConstraint("previous_target_limit BETWEEN 1 AND 8", name="previous_target_limit_range"),
        sa.CheckConstraint("target_limit BETWEEN 1 AND 8", name="target_limit_range"),
        sa.CheckConstraint("previous_group_limit BETWEEN 1 AND 8", name="previous_group_limit_range"),
        sa.CheckConstraint("group_limit BETWEEN 1 AND 8", name="group_limit_range"),
        sa.CheckConstraint("length(trim(evidence)) BETWEEN 1 AND 2048", name="evidence_length"),
        sa.ForeignKeyConstraint(["target_id"], ["asr_compute_targets.target_id"]),
        sa.ForeignKeyConstraint(["capacity_group_id"], ["asr_capacity_groups.group_id"]),
        sa.PrimaryKeyConstraint("change_id"))
    op.create_index("ix_asr_capacity_changes_target_time", "asr_capacity_changes", ["target_id", "changed_at"])
    op.create_index("ix_remote_asr_calls_profile_state", "remote_asr_calls", ["profile_id", "state"])
    op.drop_index("uq_remote_asr_calls_active_profile", table_name="remote_asr_calls")


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("ASR调用池降级须在线核对数据")
    connection = op.get_bind()
    unsafe = (
        "SELECT count(*) FROM asr_capacity_changes",
        "SELECT count(*) FROM asr_target_worker_sessions WHERE active_task_id IS NOT NULL OR child_pid IS NOT NULL OR last_error IS NOT NULL",
        "SELECT count(*) FROM asr_compute_targets WHERE target_limit<>1",
        "SELECT count(*) FROM asr_capacity_groups WHERE capacity_limit<>1",
        "SELECT count(*) FROM (SELECT profile_id FROM remote_asr_calls WHERE state IN ('held','uncertain') GROUP BY profile_id HAVING count(*)>1)",
    )
    if any(connection.scalar(sa.text(query)) for query in unsafe):
        raise RuntimeError("已有调用池审计、槽报告或多槽容量事实，拒绝有损降级；保留备份并核对恢复方案")
    op.create_index("uq_remote_asr_calls_active_profile", "remote_asr_calls", ["profile_id"],
                    unique=True, sqlite_where=sa.text("state IN ('held', 'uncertain')"))
    op.drop_index("ix_remote_asr_calls_profile_state", table_name="remote_asr_calls")
    op.drop_index("ix_asr_capacity_changes_target_time", table_name="asr_capacity_changes")
    op.drop_table("asr_capacity_changes")
    with op.batch_alter_table("asr_target_worker_sessions") as batch:
        batch.drop_column("last_error")
        batch.drop_column("child_pid")
        batch.drop_column("active_task_id")
