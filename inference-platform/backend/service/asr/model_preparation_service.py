"""显式准备固定版本模型；本地检查和转写不会隐式下载。"""

import logging

from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.asr.model_store import MANIFEST_FILENAME, ModelStore
from backend.schema.asr.asr_schema import ModelFileInfo, ModelManifest

logger = logging.getLogger(__name__)

MODEL_REPO = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
# 此固定版本的模型权重来自 Hugging Face LFS 对象元数据。
EXPECTED_MODEL_BYTES = 483546902
EXPECTED_MODEL_SHA256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
PREPARE_COMMAND = "python -m backend.modules.asr.prepare_model"


def _validate_config(settings: AppSettings) -> None:
    if settings.asr_model_repo != MODEL_REPO or settings.asr_model_revision != MODEL_REVISION:
        raise AppError("MODEL_INVALID", "当前仅支持已核验的 Whisper small 固定版本，请恢复 env.example 中的模型配置。")


def _file_manifest(store: ModelStore) -> dict[str, ModelFileInfo]:
    files = {}
    for filename in REQUIRED_MODEL_FILES:
        try:
            size, digest = store.file_info(filename)
        except OSError as exc:
            raise AppError("MODEL_INVALID", f"模型文件 {filename} 缺失或无法读取，请重新运行 {PREPARE_COMMAND}。") from exc
        if size <= 0:
            raise AppError("MODEL_INVALID", f"模型文件 {filename} 为空，请重新运行 {PREPARE_COMMAND}。")
        files[filename] = ModelFileInfo(size_bytes=size, sha256=digest)
    weights = files["model.bin"]
    if weights.size_bytes != EXPECTED_MODEL_BYTES or weights.sha256 != EXPECTED_MODEL_SHA256:
        raise AppError("MODEL_INVALID", f"model.bin 与固定版本的可信大小或 SHA256 不一致，请检查下载缓存并重新运行 {PREPARE_COMMAND}。")
    return files


def inspect_model(settings: AppSettings) -> ModelManifest:
    """仅从本地清单和实际文件核验完整性，永不访问网络。"""
    _validate_config(settings)
    store = ModelStore(settings.asr_model_dir)
    try:
        raw_manifest = store.read_manifest()
    except FileNotFoundError as exc:
        raise AppError("MODEL_NOT_READY", f"本地模型尚未准备完成，请先运行 {PREPARE_COMMAND}。", 503) from exc
    except (OSError, UnicodeError) as exc:
        raise AppError("MODEL_INVALID", f"模型清单无法读取，请检查目录权限并重新运行 {PREPARE_COMMAND}。") from exc
    try:
        manifest = ModelManifest.model_validate_json(raw_manifest)
    except ValidationError as exc:
        raise AppError("MODEL_INVALID", f"模型清单格式损坏，请重新运行 {PREPARE_COMMAND}。") from exc
    if manifest.model_repo != settings.asr_model_repo or manifest.model_revision != settings.asr_model_revision:
        raise AppError("MODEL_INVALID", f"模型清单版本与配置不一致，请重新运行 {PREPARE_COMMAND}。")
    if set(manifest.files) != set(REQUIRED_MODEL_FILES):
        raise AppError("MODEL_INVALID", f"模型清单的文件列表不完整，请重新运行 {PREPARE_COMMAND}。")
    actual_files = _file_manifest(store)
    for filename in REQUIRED_MODEL_FILES:
        if actual_files[filename] != manifest.files[filename]:
            raise AppError("MODEL_INVALID", f"模型文件 {filename} 校验失败，请重新运行 {PREPARE_COMMAND}。")
    return manifest


def prepare_model(settings: AppSettings) -> ModelManifest:
    """准备固定版本模型；校验全部通过后才发布就绪清单。"""
    _validate_config(settings)
    try:
        manifest = inspect_model(settings)
    except AppError as exc:
        if exc.code not in {"MODEL_NOT_READY", "MODEL_INVALID"}:
            raise
    else:
        logger.info("本地模型完整性检查通过，复用已有模型。")
        return manifest

    store = ModelStore(settings.asr_model_dir)
    try:
        unexpected = store.directory_entries() - set(REQUIRED_MODEL_FILES) - {MANIFEST_FILENAME}
    except OSError as exc:
        raise AppError("MODEL_INVALID", "模型目录无法访问，请检查 INFERENCE_ASR_MODEL_DIR 和目录权限。") from exc
    if unexpected:
        raise AppError("MODEL_INVALID", "模型目录含有其他文件，为保留原数据，请把 INFERENCE_ASR_MODEL_DIR 指向独立的空目录后重试。")

    logger.info("准备固定版本模型：%s@%s", settings.asr_model_repo, settings.asr_model_revision)
    try:
        snapshot = store.download_snapshot(settings.asr_model_repo, settings.asr_model_revision, REQUIRED_MODEL_FILES)
    except Exception as exc:
        raise AppError("MODEL_DOWNLOAD_FAILED", "模型下载失败，未生成就绪清单；请检查网络、磁盘空间后重试，已下载缓存会保留。", 503) from exc
    try:
        with store.stage_snapshot(snapshot, REQUIRED_MODEL_FILES) as staged:
            manifest = ModelManifest(
                model_repo=settings.asr_model_repo,
                model_revision=settings.asr_model_revision,
                files=_file_manifest(staged),
            )
            staged.write_manifest(manifest.model_dump_json(by_alias=True, indent=2) + "\n")
            store.publish_staged(staged)
    except OSError as exc:
        raise AppError("MODEL_INVALID", "模型文件准备失败，未发布新的就绪模型；请检查文件完整性、磁盘空间和目录权限后重试。") from exc
    logger.info("模型准备完成，全部必需文件及固定权重 SHA256 已验证。")
    return manifest
