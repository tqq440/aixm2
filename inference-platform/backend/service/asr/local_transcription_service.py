"""本地验证流程：检查结果路径、调用 ASR、保存完整结果。"""

from pathlib import Path

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.asr.result_store import write_result
from backend.schema.asr.asr_schema import TranscriptionRequest, TranscriptionResult
from backend.service.asr.adapter_catalog import configured_asr_plugin


def transcribe_to_file(settings: AppSettings, request: TranscriptionRequest, output_path: Path) -> TranscriptionResult:
    if output_path.suffix.lower() != ".json":
        raise AppError("RESULT_PATH_INVALID", "结果文件请使用 .json 扩展名。")
    if output_path.exists():
        raise AppError("RESULT_EXISTS", "结果文件已经存在，请换一个名称；不会覆盖已有文件。", 409)
    result = configured_asr_plugin(settings).create(settings).transcribe(request)
    try:
        write_result(output_path, result.model_dump_json(by_alias=True, indent=2) + "\n")
    except FileExistsError as exc:
        raise AppError("RESULT_EXISTS", "结果文件已被其他操作创建，请换一个名称。", 409) from exc
    except OSError as exc:
        raise AppError("RESULT_WRITE_FAILED", "转写完成但结果文件保存失败，请检查磁盘空间和目录权限。", 500) from exc
    return result
