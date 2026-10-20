"""固定计算目标执行槽的本机独占身份；容量和任务状态由对应 Service 管理。"""

from pathlib import Path

from filelock import FileLock, Timeout

from backend.schema.asr.target_schema import TargetBinding
from backend.utils.json.json_utils import canonical_json_sha256


class TargetSlotLock:
    """同一数据库、目标编号与槽位始终共用锁，不因模型或目标修订改变。"""

    def __init__(self, database_path: Path, target_id: str, slot_index: int) -> None:
        binding = TargetBinding(target_id=target_id, slot_index=slot_index)
        digest = canonical_json_sha256({
            "identityScheme": "asr-target-slot-v1",
            "targetId": binding.target_id,
            "slotIndex": binding.slot_index,
        })
        database = Path(database_path).resolve()
        # 用户可见编号不参与路径拼接，锁文件只使用固定前缀与十六进制摘要。
        self._path = database.with_name(database.name + ".target-slot-" + digest + ".lock")
        self._worker_key = "target-slot-" + digest[:48]
        self._lock = FileLock(self._path, timeout=0)

    @property
    def worker_key(self) -> str:
        return self._worker_key

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """立即尝试独占；已由本对象持有时不累加重入次数，IO错误不伪装成忙碌。"""
        if self._lock.is_locked:
            return True
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            return False
        return True

    def release(self) -> None:
        """由 FileLock 释放本对象的锁；不确认任务或远端执行结束。"""
        self._lock.release()
