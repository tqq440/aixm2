"""外部 ASR 调用事实与运行类型；不登记虚构模型或服务。"""

from alembic import context, op
import sqlalchemy as sa

revision = "20261015_0010"
down_revision = "20261014_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("remote_asr_calls",
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("profile_id", sa.String(64), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.String(512), nullable=True),
        sa.CheckConstraint("state IN ('queued', 'held', 'released', 'uncertain')", name="state_allowed"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
        sa.PrimaryKeyConstraint("task_id"))
    op.create_index("uq_remote_asr_calls_active_profile", "remote_asr_calls", ["profile_id"],
                    unique=True, sqlite_where=sa.text("state IN ('held', 'uncertain')"))
    with op.batch_alter_table("runtime_components") as batch:
        batch.drop_constraint(op.f("ck_runtime_components_component_id_allowed"), type_="check")
        batch.create_check_constraint("component_id_allowed",
            "component_id IN ('worker', 'vision_worker', 'dispatcher', 'remote_asr_worker')")


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("外部 ASR 降级须在线核对数据")
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM remote_asr_calls")) or connection.scalar(
            sa.text("SELECT count(*) FROM runtime_components WHERE component_id='remote_asr_worker'")):
        raise RuntimeError("已有外部调用或运行记录，拒绝有损降级；请保留备份并核对恢复方案")
    with op.batch_alter_table("runtime_components") as batch:
        batch.drop_constraint(op.f("ck_runtime_components_component_id_allowed"), type_="check")
        batch.create_check_constraint("component_id_allowed", "component_id IN ('worker', 'vision_worker', 'dispatcher')")
    op.drop_index("uq_remote_asr_calls_active_profile", table_name="remote_asr_calls")
    op.drop_table("remote_asr_calls")
