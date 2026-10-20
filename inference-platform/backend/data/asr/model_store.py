"""模型下载缓存、暂存和本地清单读写；完整性规则由 Service 决定。"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from backend.config.settings.model_environment import configure_model_cache
from backend.utils.file.file_utils import atomic_write_text, sha256_file

MANIFEST_FILENAME = "manifest.json"


class ModelStore:
    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir.resolve()

    def read_manifest(self) -> str:
        return (self.model_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")

    def write_manifest(self, content: str) -> None:
        atomic_write_text(self.model_dir / MANIFEST_FILENAME, content)

    def file_info(self, filename: str) -> tuple[int, str]:
        path = self.model_dir / filename
        return path.stat().st_size, sha256_file(path)

    def directory_entries(self) -> set[str]:
        if not self.model_dir.exists():
            return set()
        return {entry.name for entry in self.model_dir.iterdir()}

    def download_snapshot(self, repo: str, revision: str, filenames: Sequence[str]) -> Path:
        # 延迟导入：启动 HTTP 后台、读取已有清单不需要连接模型仓库。
        cache_dir = configure_model_cache(self.model_dir)
        from huggingface_hub import snapshot_download

        self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        return Path(snapshot_download(
            repo_id=repo,
            revision=revision,
            allow_patterns=list(filenames),
            cache_dir=str(cache_dir),
            token=False,
        ))

    @contextmanager
    def stage_snapshot(self, snapshot: Path, filenames: Sequence[str]) -> Iterator["ModelStore"]:
        """只自动清理本次创建的临时目录，不删除原模型目录或下载缓存。"""
        self.model_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{self.model_dir.name}-stage-", dir=self.model_dir.parent,
        ) as temporary_dir:
            staged_dir = Path(temporary_dir) / "model"
            staged_dir.mkdir()
            for filename in filenames:
                shutil.copyfile(snapshot / filename, staged_dir / filename)
            yield ModelStore(staged_dir)

    def publish_staged(self, staged: "ModelStore") -> None:
        """安装已校验目录；旧目录保留为备份，失败时尝试恢复原位置。"""
        target_parent = self.model_dir.parent.resolve()
        staged_dir = staged.model_dir.resolve()
        if (
            staged_dir.parent.parent != target_parent
            or not staged_dir.parent.name.startswith(f".{self.model_dir.name}-stage-")
            or staged_dir.name != "model"
        ):
            raise ValueError("模型暂存目录必须是本次模型目录旁的受管目录")
        backup = target_parent / f"{self.model_dir.name}.backup-{uuid4().hex}"
        had_previous = self.model_dir.exists()
        if had_previous:
            self.model_dir.replace(backup)
        try:
            staged_dir.replace(self.model_dir)
        except OSError:
            if had_previous and not self.model_dir.exists():
                backup.replace(self.model_dir)
            raise
