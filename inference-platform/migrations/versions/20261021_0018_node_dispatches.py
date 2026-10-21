"""中心绑定 Node 执行身份与迟到结果；旧任务和许可不变。"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_0018"
down_revision = "20261021_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inference_node_dispatches",
        sa.Column("invocation_id", sa.String(32), sa.ForeignKey("inference_invocations.invocation_id"), primary_key=True),
        sa.Column("command_json", sa.JSON(), nullable=False), sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("terminal_hash", sa.String(64)), sa.Column("remote_state", sa.String(16)),
        sa.Column("late_result_json", sa.JSON()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    connection = op.get_bind()
    if (connection.scalar(sa.text("SELECT COUNT(*) FROM inference_node_dispatches"))
            or connection.scalar(sa.text("SELECT COUNT(*) FROM inference_invocations"))):
        # 旧公共链本来也会拒绝非空降级，在删新表/移动 head 前就检查，避免留下中间版本。
        raise RuntimeError("公共调用或 Node 派发已有记录，拒绝删除身份与迟到结果")
    op.drop_table("inference_node_dispatches")
