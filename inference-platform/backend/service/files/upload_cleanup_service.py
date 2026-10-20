"""M06 孤立对象维护；任务引用比文件年龄优先，共用上传锁保护在途对象。"""

from datetime import datetime, timedelta, timezone
import logging

from filelock import Timeout
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.data.files.upload_store import LocalUploadStore
from backend.data.tasks.task_repository import TaskRepository
from backend.schema.files.cleanup_schema import CleanupOptions, CleanupReport


def clean_uploads(settings: AppSettings, options: CleanupOptions, logger: logging.Logger) -> CleanupReport:
    database = None
    try:
        database = Database(settings)
        repository = TaskRepository(database)
        store = LocalUploadStore(settings.upload_dir)
        with store.lock():
            # 引用读取失败必须停止清理，不把数据库故障误当作“没有引用”。
            references = repository.referenced_input_refs()
            candidates = store.iter_orphan_candidates(
                references, datetime.now(timezone.utc) - timedelta(hours=24),
            )
            deleted = 0
            if options.apply:
                for path in candidates:
                    store.delete_candidate(path)
                    deleted += 1
            logger.info("上传目录维护 candidates=%d deleted=%d", len(candidates), deleted)
            return CleanupReport(applied=options.apply, candidates=[path.name for path in candidates], deleted=deleted)
    except SQLAlchemyError as exc:
        raise AppError("TASK_STORAGE_UNAVAILABLE", "无法核对任务引用，已停止清理。请检查数据库和迁移。", 503) from exc
    except Timeout as exc:
        raise AppError("UPLOAD_BUSY", "音频正在保存，未取得维护锁，请稍后重试。", 503) from exc
    except (OSError, ValueError) as exc:
        raise AppError("UPLOAD_CLEANUP_FAILED", "文件维护未完成，请检查日志和目录权限后重新预览。", 503) from exc
    finally:
        if database is not None:
            database.dispose()
