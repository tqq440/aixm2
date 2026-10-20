"""扩展任务输入与独立 Vision Worker 身份，保留原 ASR 档案和模型目录。

Revision ID: 20261013_0006
Revises: 20261013_0005
"""

from alembic import op
import sqlalchemy as sa


revision = "20261013_0006"
down_revision = "20261013_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("input_media_type", sa.String(16), server_default="audio", nullable=False))
        batch.add_column(sa.Column("image_width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("image_height", sa.Integer(), nullable=True))
        batch.alter_column("audio_duration_seconds", existing_type=sa.Float(), nullable=True)
        batch.create_check_constraint("task_type_allowed", "task_type IN ('asr', 'vision')")
        batch.create_check_constraint(
            "input_metadata_matches_task_type",
            "(task_type = 'asr' AND input_media_type = 'audio' "
            "AND audio_duration_seconds IS NOT NULL AND image_width IS NULL AND image_height IS NULL) "
            "OR (task_type = 'vision' AND input_media_type = 'image' "
            "AND audio_duration_seconds IS NULL AND image_width IS NOT NULL AND image_width > 0 "
            "AND image_height IS NOT NULL AND image_height > 0)",
        )
    with op.batch_alter_table("runtime_components") as batch:
        batch.drop_constraint(op.f("ck_runtime_components_component_id_allowed"), type_="check")
        batch.create_check_constraint("component_id_allowed", "component_id IN ('worker', 'vision_worker', 'dispatcher')")


def downgrade() -> None:
    connection = op.get_bind()
    vision_tasks = connection.scalar(sa.text("SELECT COUNT(*) FROM tasks WHERE task_type = 'vision'"))
    vision_runtime = connection.scalar(sa.text("SELECT COUNT(*) FROM runtime_components WHERE component_id = 'vision_worker'"))
    if vision_tasks or vision_runtime:
        raise RuntimeError("存在视觉任务或 Vision Worker 运行记录，不能降级至 0005；请先停止服务并备份，不会自动删除这些记录。")
    with op.batch_alter_table("runtime_components") as batch:
        batch.drop_constraint(op.f("ck_runtime_components_component_id_allowed"), type_="check")
        batch.create_check_constraint("component_id_allowed", "component_id IN ('worker', 'dispatcher')")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint(op.f("ck_tasks_input_metadata_matches_task_type"), type_="check")
        batch.drop_constraint(op.f("ck_tasks_task_type_allowed"), type_="check")
        batch.alter_column("audio_duration_seconds", existing_type=sa.Float(), nullable=False)
        batch.drop_column("image_height")
        batch.drop_column("image_width")
        batch.drop_column("input_media_type")
