"""独立 Node 资料与上报快照；不是中心平台 0018。"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_node_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("host_node_identities", sa.Column("identity_id", sa.Integer(), primary_key=True),
        sa.Column("node_id", sa.String(64), nullable=False, unique=True), sa.Column("epoch_id", sa.String(36)),
        sa.Column("started_at", sa.DateTime()), sa.Column("stopped_at", sa.DateTime()))
    op.create_table("host_node_services", sa.Column("service_id", sa.String(64), primary_key=True),
        sa.Column("config_revision", sa.String(64), nullable=False), sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("current_instance_id", sa.String(36)), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_table("host_node_service_revisions", sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id"), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False), sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("service_id", "revision"))
    op.create_table("host_node_instances", sa.Column("instance_id", sa.String(36), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id"), nullable=False),
        sa.Column("config_revision", sa.String(64), nullable=False), sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("report_hash", sa.String(64)),
        sa.Column("reported_state", sa.String(24), nullable=False), sa.Column("model_ready", sa.Boolean(), nullable=False),
        sa.Column("accepting_requests", sa.Boolean(), nullable=False), sa.Column("health", sa.String(16), nullable=False),
        sa.Column("queued_tasks", sa.Integer()), sa.Column("active_tasks", sa.Integer()),
        sa.Column("error_code", sa.String(64)), sa.Column("reported_at", sa.DateTime()),
        sa.Column("received_at", sa.DateTime()), sa.Column("node_epoch_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("service_id", "generation"))
    op.create_table("host_node_events", sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("host_node_services.service_id")),
        sa.Column("instance_id", sa.String(36)), sa.Column("config_revision", sa.String(64)),
        sa.Column("event_type", sa.String(40), nullable=False), sa.Column("source", sa.String(16), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_host_node_events_service_id", "host_node_events", ["service_id"])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM host_node_events")):
        raise RuntimeError("Node 已有记录，拒绝删除历史；请使用前向修复或成套恢复。")
    op.drop_table("host_node_events")
    op.drop_table("host_node_instances")
    op.drop_table("host_node_service_revisions")
    op.drop_table("host_node_services")
    op.drop_table("host_node_identities")
