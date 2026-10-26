"""恢复副本只作核对，禁止从恢复档案直接启动控制或执行。"""

import os
from pathlib import Path
from backend.common.exceptions.app_error import StartupError


def require_active_database(path):
    path=Path(path)
    marker=Path(str(path)+".restore-quarantine.json")
    pending=any(parent.name.casefold().startswith((".pending-snapshot-",".pending-restore-")) for parent in path.parents)
    restored=any(os.path.lexists(parent/"RESTORE_QUARANTINED.json") for parent in path.parents)
    if pending or restored or os.path.lexists(marker):
        raise StartupError("RESTORE_QUARANTINED：该库是隔离恢复副本；尚未完成切换核对，不能启动或派发。")
