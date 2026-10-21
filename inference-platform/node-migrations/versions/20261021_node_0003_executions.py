"""Node 内部执行身份、去重及结果恢复档案；不写中心库。"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_node_0003"
down_revision = "20261021_node_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("host_node_executions",
        sa.Column("invocation_id", sa.String(32), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id"), nullable=False),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("host_node_instances.instance_id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("command_json", sa.JSON(), nullable=False),
        sa.Column("node_epoch_id", sa.String(36), nullable=False), sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result_json", sa.JSON()), sa.Column("result_hash", sa.String(64)), sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('sending','succeeded','failed','unknown')", name="execution_state"))
    op.create_index("ix_host_node_executions_service_state", "host_node_executions", ["service_id", "state"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM host_node_executions")):
        raise RuntimeError("执行恢复档案非空，拒绝删除去重身份和未知占位")
    op.drop_table("host_node_executions")
