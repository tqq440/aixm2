"""中心只读 Node 监控快照；不重绑定旧任务。"""

from alembic import op
import sqlalchemy as sa

revision="20261021_0019"
down_revision="20261021_0018"
branch_labels=None
depends_on=None


def upgrade():
    op.add_column("inference_node_dispatches",sa.Column("monitoring_json",sa.JSON(),nullable=False,server_default="{}"))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM inference_invocations")):
        raise RuntimeError("公共调用历史非空，拒绝删除监控快照")
    with op.batch_alter_table("inference_node_dispatches") as batch:batch.drop_column("monitoring_json")
