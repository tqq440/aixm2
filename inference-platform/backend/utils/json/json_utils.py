"""比较已验证的 JSON 值，不沿用 Python 将 bool 视作 int 的规则。"""

from hashlib import sha256
import json


def canonical_json_sha256(value: object) -> str:
    """对 JSON 值做稳定摘要；对象键顺序不改变摘要，不接受非有限数字。"""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def json_values_equal(left: object, right: object) -> bool:
    """对象键顺序无关、数组顺序有关，数字 1 和 1.0 等价，但 true 和 1 不等价。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if left is None or right is None:
        return left is right
    if isinstance(left, (int, float)):
        return isinstance(right, (int, float)) and left == right
    if isinstance(left, str):
        return isinstance(right, str) and left == right
    if isinstance(left, list):
        return (isinstance(right, list) and len(left) == len(right)
                and all(json_values_equal(first, second) for first, second in zip(left, right)))
    if isinstance(left, dict):
        return (isinstance(right, dict) and left.keys() == right.keys()
                and all(json_values_equal(value, right[key]) for key, value in left.items()))
    return False
