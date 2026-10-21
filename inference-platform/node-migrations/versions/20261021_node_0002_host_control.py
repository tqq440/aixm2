"""主机控制操作、固定版本容器资料与资源快照，仅独立 Node 库。"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_node_0002"
down_revision = "20261021_node_0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("host_node_services") as batch:
        batch.add_column(sa.Column("runtime_revision", sa.String(64)))
        batch.add_column(sa.Column("control_version", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("control_json", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("host_node_instances") as batch:
        batch.add_column(sa.Column("control_report_json", sa.JSON()))
        batch.add_column(sa.Column("exit_confirmed_at", sa.DateTime()))
        # 升级前无法证明实例从未接单；只有受控暂停启动的新实例可从 false 开始。
        batch.add_column(sa.Column("may_have_accepted", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table("host_node_runtime_revisions", sa.Column("runtime_id", sa.String(36), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id"), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False), sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("service_id", "revision"))
    op.create_table("host_node_operations", sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id"), nullable=False),
        sa.Column("action", sa.String(16), nullable=False), sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("runtime_revision", sa.String(64), nullable=False), sa.Column("config_revision", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False), sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_host_node_operations_service_id", "host_node_operations", ["service_id"])
    op.create_table("host_node_resource_snapshots", sa.Column("snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("node_epoch_id", sa.String(36), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(), nullable=False))


def downgrade():
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {name}")) for name in
           ("host_node_operations", "host_node_runtime_revisions", "host_node_resource_snapshots")):
        raise RuntimeError("主机控制已有历史，拒绝降级删除；使用前向修复或成套恢复。")
    for name in ("host_node_resource_snapshots", "host_node_operations", "host_node_runtime_revisions"):
        op.drop_table(name)
    with op.batch_alter_table("host_node_instances") as batch:
        batch.drop_column("may_have_accepted")
        batch.drop_column("exit_confirmed_at")
        batch.drop_column("control_report_json")
    with op.batch_alter_table("host_node_services") as batch:
        batch.drop_column("control_json")
        batch.drop_column("control_version")
        batch.drop_column("runtime_revision")
