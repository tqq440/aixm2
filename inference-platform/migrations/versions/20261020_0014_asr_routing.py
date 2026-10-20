"""新增 ASR 路由快照与任务选择；旧任务、调用指纹及许可均不回填。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261020_0014"
down_revision = "20261020_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("asr_routing_revisions",
        sa.Column("routing_revision_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("evidence_kind", sa.String(16), nullable=False),
        sa.Column("snapshot", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(routing_revision_id) BETWEEN 1 AND 64", name="routing_revision_id_length"),
        sa.CheckConstraint("length(model_id) BETWEEN 1 AND 64", name="model_id_length"),
        sa.CheckConstraint("length(model_version) BETWEEN 1 AND 64", name="model_version_length"),
        sa.CheckConstraint("evidence_kind IN ('provided', 'isolated')", name="evidence_kind_allowed"),
        sa.PrimaryKeyConstraint("routing_revision_id"))
    op.create_index("ix_asr_routing_revisions_model_created", "asr_routing_revisions",
                    ["model_id", "model_version", "created_at"])
    op.create_table("asr_task_routes",
        sa.Column("task_id", sa.String(32), nullable=False),
        sa.Column("routing_revision_id", sa.String(64), nullable=False),
        sa.Column("selected_catalog_revision_id", sa.String(64), nullable=True),
        sa.Column("decision_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("selected_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("length(task_id) = 32", name="task_id_length"),
        sa.CheckConstraint(
            "(selected_catalog_revision_id IS NULL AND selected_at IS NULL) OR "
            "(selected_catalog_revision_id IS NOT NULL AND selected_at IS NOT NULL AND decision_snapshot IS NOT NULL)",
            name="selection_fields_consistent"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
        sa.ForeignKeyConstraint(["routing_revision_id"], ["asr_routing_revisions.routing_revision_id"]),
        sa.ForeignKeyConstraint(["selected_catalog_revision_id"], ["asr_catalog_revisions.catalog_revision_id"]),
        sa.PrimaryKeyConstraint("task_id"))
    op.create_index("ix_asr_task_routes_revision_task", "asr_task_routes", ["routing_revision_id", "task_id"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("ASR 路由降级须在线核对数据")
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f'SELECT count(*) FROM "{name}"'))
           for name in ("asr_routing_revisions", "asr_task_routes")):
        raise RuntimeError("已有 ASR 路由或任务选择记录，拒绝有损降级；请保留备份并核对恢复方案")
    op.drop_index("ix_asr_task_routes_revision_task", table_name="asr_task_routes")
    op.drop_table("asr_task_routes")
    op.drop_index("ix_asr_routing_revisions_model_created", table_name="asr_routing_revisions")
    op.drop_table("asr_routing_revisions")
