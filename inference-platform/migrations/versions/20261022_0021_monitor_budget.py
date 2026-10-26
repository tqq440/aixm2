"""Persistent cross-Node monitoring budget, independent of inference permits."""

from alembic import op
import sqlalchemy as sa

revision = "20261022_0021"
down_revision = "20261021_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("monitoring_permits",
        sa.Column("poll_id", sa.String(32), primary_key=True),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("invocation_id", sa.String(32), nullable=False),
        sa.Column("service_key", sa.String(64), nullable=False),
        sa.Column("endpoint_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True))
    op.create_index("ix_monitoring_permits_state_service_key", "monitoring_permits", ["state", "service_key"])
    op.create_index("ix_monitoring_permits_state_endpoint_key", "monitoring_permits", ["state", "endpoint_key"])


def downgrade():
    # 带占位账本不得在降级中丢失；需先显式核对原查询。
    if op.get_bind().execute(sa.text("SELECT count(*) FROM monitoring_permits WHERE state='held'")).scalar():
        raise RuntimeError("MONITOR_BUDGET_HELD: cannot discard unresolved query permits")
    op.drop_index("ix_monitoring_permits_state_service_key", table_name="monitoring_permits")
    op.drop_index("ix_monitoring_permits_state_endpoint_key", table_name="monitoring_permits")
    op.drop_table("monitoring_permits")
