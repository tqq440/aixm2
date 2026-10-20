"""有大小上限的严格JSON文件读取；不执行内容。"""

import json
from pathlib import Path
import re


def read_bounded_json(path: Path, *, max_bytes: int) -> object:
    """拒绝重复键、非有限JSON常量和过大文件，保留原始类型供Schema校验。"""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Invalid JSON byte limit")
    # 离线输入仅支持本机路径；不经UNC或URL读取远端资料。
    if str(path).startswith(("\\\\", "//")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", str(path)):
        raise ValueError("A local JSON file is required")
    resolved = path.resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("A local JSON file is required")
    with resolved.open("rb") as stream:
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("JSON input exceeds byte limit")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("Non-finite JSON constant")

    return json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique_keys,
                      parse_constant=nonfinite)
