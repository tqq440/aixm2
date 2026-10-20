"""对 UUID 做确定性规范化和带命名空间的单向摘要。"""

from hashlib import sha256
from uuid import UUID


def hash_namespaced_uuid(value: str, *, namespace: str) -> str:
    """相同 UUID 的大小写/括号形式得到相同摘要，错误不带原始值。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("身份 UUID 为空或类型无效。")
    try:
        canonical = UUID(value.strip())
    except (ValueError, AttributeError):
        raise ValueError("身份 UUID 格式无效。") from None
    if canonical.int == 0:
        raise ValueError("身份 UUID 不能使用空 UUID。")
    return sha256(f"{namespace}:{canonical}".encode("utf-8")).hexdigest()
