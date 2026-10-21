"""公共服务目录第一模块；不改写原任务、许可、模型及 ASR 配置。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261020_0015"
down_revision = "20261020_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inference_services",
        sa.Column("service_id", sa.String(64), primary_key=True),
        sa.Column("draft_revision", sa.String(64), nullable=False),
        sa.Column("active_revision", sa.String(64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="positive_version"))
    op.create_table("inference_service_revisions",
        sa.Column("revision", sa.String(64), primary_key=True),
        sa.Column("service_id", sa.String(64), sa.ForeignKey("inference_services.service_id"), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_inference_service_revisions_service_created", "inference_service_revisions", ["service_id", "created_at"])
    op.create_table("inference_resource_groups",
        sa.Column("group_id", sa.String(64), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="positive_version"))
    op.create_table("inference_target_checks",
        sa.Column("check_id", sa.String(32), primary_key=True),
        sa.Column("revision", sa.String(64), sa.ForeignKey("inference_service_revisions.revision"), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("revision", "target_id"))
    op.create_table("inference_catalog_events",
        sa.Column("event_id", sa.String(32), primary_key=True),
        sa.Column("service_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("revision", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_inference_catalog_events_service_time", "inference_catalog_events", ["service_id", "created_at"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("服务目录降级须在线核对，不能丢弃资料")
    connection = op.get_bind()
    tables = ["inference_catalog_events", "inference_target_checks", "inference_service_revisions",
              "inference_resource_groups", "inference_services"]
    if any(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {name}")) for name in tables):
        raise RuntimeError("公共目录已有资料，请保留备份并使用前向修复，拒绝删除历史")
    op.drop_index("ix_inference_catalog_events_service_time", table_name="inference_catalog_events")
    op.drop_table("inference_catalog_events")
    op.drop_table("inference_target_checks")
    op.drop_index("ix_inference_service_revisions_service_created", table_name="inference_service_revisions")
    op.drop_table("inference_service_revisions")
    op.drop_table("inference_resource_groups")
    op.drop_table("inference_services")
