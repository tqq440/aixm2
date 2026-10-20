"""建立迁移基线；第一部分不创建业务表。

Revision ID: 20261012_0001
Revises:
Create Date: 2026-10-12
"""

revision: str = "20261012_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """仅由 Alembic 记录当前版本，不提前增加业务结构。"""
    pass


def downgrade() -> None:
    """基线没有业务结构需要移除。"""
    pass
