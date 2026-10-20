"""新增持久化模型目录；保留任务快照和所有历史结果。

Revision ID: 20261013_0005
Revises: 20261012_0004
Create Date: 2026-10-13
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "20261013_0005"
down_revision = "20261012_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    models = op.create_table(
        "models",
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("default_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("model_id", name=op.f("pk_models")),
    )
    versions = op.create_table(
        "model_versions",
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["model_id"], ["models.model_id"], name=op.f("fk_model_versions_model_id_models")),
        sa.PrimaryKeyConstraint("model_id", "model_version", name=op.f("pk_model_versions")),
    )
    # 数据迁移固定当时已经验证的版本；不能导入会随未来开发变化的 Service 或模型设置。
    version = "536b0662742c02347bc0e980a01041f333bce120"
    languages = sorted((
        "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr "
        "gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv "
        "mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so "
        "sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh"
    ).split())
    op.bulk_insert(models, [{"model_id": "whisper-small", "default_version": version}])
    op.bulk_insert(versions, [{
        "model_id": "whisper-small", "model_version": version, "enabled": True,
        "spec_json": op.inline_literal(json.dumps({
            "modelId": "whisper-small", "modelVersion": version,
            "modelRepo": "Systran/faster-whisper-small", "taskType": "asr", "executionMode": "offline",
            "parameterSchema": {"defaultLanguage": "zh", "autoDetectionValue": "auto", "languages": languages},
            "inputConstraints": {"maxFileBytes": 20971520, "maxAudioSeconds": 300.0},
            "outputCapabilities": ["text", "segments", "language"], "adapterKey": "faster-whisper",
            "resourceRequirements": {"device": "cpu", "computeType": "int8", "maxConcurrency": 1},
        }, ensure_ascii=False)),
    }], multiinsert=False)


def downgrade() -> None:
    # 仅删除目录；任务没有指向目录表的外键，其已受理版本/输入/结果均保留。
    op.drop_table("model_versions")
    op.drop_table("models")
