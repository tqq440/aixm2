"""显式模型下载进程使用项目缓存；在第一次导入 huggingface_hub 前调用。"""

import os
import sys
from pathlib import Path


def configure_model_cache(model_dir: Path) -> Path:
    cache_dir = model_dir.parent / ".huggingface-cache"
    xet_dir = cache_dir / "xet"
    # 仅修改当前进程，不改系统环境或用户全局配置。
    os.environ["HF_XET_CACHE"] = str(xet_dir)
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        # 同一进程此前用过 HF 时，其常量可能已在首次导入时冻结。
        constants.HF_XET_CACHE = str(xet_dir)
    return cache_dir
