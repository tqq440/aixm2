"""逐调用监控与显式取消终态，旧执行身份及结果保持。"""

from alembic import op
import sqlalchemy as sa

revision="20261021_node_0004"
down_revision="20261021_node_0003"
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table("host_node_executions") as batch:
        batch.add_column(sa.Column("monitoring_json",sa.JSON(),nullable=False,server_default="{}"))
        batch.drop_constraint("execution_state",type_="check")
        batch.create_check_constraint("execution_state","state IN ('sending','succeeded','failed','cancelled','unknown')")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM host_node_executions")):
        raise RuntimeError("执行历史非空，拒绝删除监控及取消证据")
    with op.batch_alter_table("host_node_executions") as batch:
        batch.drop_constraint("execution_state",type_="check")
        batch.drop_column("monitoring_json")
        batch.create_check_constraint("execution_state","state IN ('sending','succeeded','failed','unknown')")
