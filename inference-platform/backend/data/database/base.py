"""共享 ORM 元数据、约束命名和安全的列字段转换。"""

from typing import Any

from sqlalchemy import MetaData, inspect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })

    def to_dict(self) -> dict[str, Any]:
        """只读取列，不展开关系；日期等 API 序列化由 Schema 负责。"""
        return {column.key: getattr(self, column.key) for column in inspect(self).mapper.column_attrs}
