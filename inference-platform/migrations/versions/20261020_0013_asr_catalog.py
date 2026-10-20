"""新增不可覆盖目录修订与追加式资源观测；不改写旧目标和许可。"""

from alembic import context, op
import sqlalchemy as sa


revision = "20261020_0013"
down_revision = "20261019_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("asr_catalog_revisions",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("catalog_revision_id", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("revision_id", sa.String(64), nullable=False),
        sa.Column("metadata_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(catalog_revision_id) BETWEEN 1 AND 64", name="catalog_revision_id_length"),
        sa.ForeignKeyConstraint(["target_id"], ["asr_compute_targets.target_id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["asr_target_revisions.revision_id"]),
        sa.UniqueConstraint("catalog_revision_id"),
        sa.PrimaryKeyConstraint("sequence"),
        sqlite_autoincrement=True)
    op.create_index("ix_asr_catalog_revisions_target_sequence", "asr_catalog_revisions", ["target_id", "sequence"])
    op.create_table("asr_resource_observations",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("observation_id", sa.String(64), nullable=False),
        sa.Column("catalog_revision_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("evidence_kind", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("length(observation_id) BETWEEN 1 AND 64", name="observation_id_length"),
        sa.CheckConstraint("scope IN ('target', 'capacity_group')", name="scope_allowed"),
        sa.CheckConstraint("length(trim(source)) BETWEEN 1 AND 64", name="source_length"),
        sa.CheckConstraint("evidence_kind IN ('provided', 'isolated')", name="evidence_kind_allowed"),
        sa.CheckConstraint("ttl_seconds BETWEEN 1 AND 3600", name="ttl_seconds_range"),
        sa.ForeignKeyConstraint(["catalog_revision_id"], ["asr_catalog_revisions.catalog_revision_id"]),
        sa.UniqueConstraint("observation_id"),
        sa.PrimaryKeyConstraint("sequence"),
        sqlite_autoincrement=True)
    op.create_index("ix_asr_resource_observations_catalog_scope_time", "asr_resource_observations",
                    ["catalog_revision_id", "scope", "observed_at", "sequence"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("ASR 目录降级须在线核对数据")
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f'SELECT count(*) FROM "{name}"'))
           for name in ("asr_catalog_revisions", "asr_resource_observations")):
        raise RuntimeError("已有 ASR 目录修订或资源观测，拒绝有损降级；请保留备份并核对恢复方案")
    op.drop_index("ix_asr_resource_observations_catalog_scope_time", table_name="asr_resource_observations")
    op.drop_table("asr_resource_observations")
    op.drop_index("ix_asr_catalog_revisions_target_sequence", table_name="asr_catalog_revisions")
    op.drop_table("asr_catalog_revisions")
