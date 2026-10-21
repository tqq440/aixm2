"""公共应用 Key，保留现有任务和应用额度。"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_0020"
down_revision = "20261021_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("gateway_api_keys",
        sa.Column("key_id", sa.String(32), primary_key=True),
        sa.Column("application_id", sa.String(64), sa.ForeignKey("inference_application_budgets.application_id"), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True))
    op.create_index("ix_gateway_api_keys_application", "gateway_api_keys", ["application_id", "created_at"])
    op.create_table("gateway_node_observations",
        sa.Column("node_key",sa.String(64),primary_key=True),
        sa.Column("snapshot",sa.JSON(),nullable=False),sa.Column("checked_at",sa.DateTime(),nullable=False))


def downgrade():
    if (op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM gateway_api_keys")) or
            op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM inference_invocations"))):
        raise RuntimeError("调用或凭据历史非空，拒绝删除撤销及应用归属记录")
    op.drop_table("gateway_api_keys")
    op.drop_table("gateway_node_observations")
