"""手工接口配置的统一解析；配置选择仅在进程启动时应用。"""

from backend.config.settings.app_settings import AppSettings
from backend.data.asr.service_config_store import ServiceConfigStore
from backend.schema.asr.service_config_schema import AsrServiceConfig, ServiceConfigDocument
from backend.utils.json.json_utils import canonical_json_sha256


def service_revision(config: AsrServiceConfig) -> str:
    return canonical_json_sha256(config.model_dump(mode="json", by_alias=True))


def read_service_document(settings: AppSettings) -> ServiceConfigDocument:
    document = ServiceConfigDocument.model_validate(ServiceConfigStore(settings.database_path).read())
    for revision, config in document.revisions.items():
        if revision != service_revision(config):
            raise ValueError("ASR configuration revision mismatch")
    for service_id, revision in document.items.items():
        if revision not in document.revisions or document.revisions[revision].service_id != service_id:
            raise ValueError("ASR configuration index mismatch")
    if document.selected_revision is not None and document.selected_revision not in document.revisions:
        raise ValueError("ASR selected revision missing")
    return document


def settings_for_service(settings: AppSettings, config: AsrServiceConfig) -> AppSettings:
    # model_dump 排除原有 SecretStr；保留原LLM/Agent密钥对象，不能因ASR切换而丢失。
    values = {name: getattr(settings, name) for name in type(settings).model_fields}
    values.update(
        remote_asr_base_url=config.base_url, remote_asr_model_id=config.model_id,
        remote_asr_model_version=config.model_version, remote_asr_ca_file=config.ca_file,
        remote_asr_insecure_tls_origin=config.insecure_tls_origin,
        remote_asr_protocol=config.protocol, remote_asr_transcribe_path=config.transcribe_path,
        remote_asr_health_path=config.health_path, remote_asr_upstream_model=config.upstream_model,
        remote_asr_api_key_env=config.api_key_env,
        remote_asr_connect_timeout_seconds=config.connect_timeout_seconds,
        remote_asr_total_timeout_seconds=config.total_timeout_seconds,
        remote_asr_routing_revision=None, remote_asr_routing_isolation=False,
        asr_service_id=config.service_id, asr_service_revision=service_revision(config),
    )
    return AppSettings.model_validate(values)


def apply_service_selection(settings: AppSettings) -> AppSettings:
    # 平台管理器给子进程固定修订，避免启动期间选择变化产生混用配置。
    if settings.asr_service_revision == "legacy":
        return settings.model_copy(update={"asr_service_revision": None, "asr_service_id": None})
    document = read_service_document(settings)
    revision = settings.asr_service_revision or document.selected_revision
    if revision is None:
        return settings
    if revision not in document.revisions:
        raise ValueError("Pinned ASR service revision missing")
    return settings_for_service(settings, document.revisions[revision])


def current_service_config(settings: AppSettings) -> AsrServiceConfig | None:
    if not settings.remote_asr_base_url or not settings.remote_asr_model_id:
        return None
    return AsrServiceConfig(
        service_id=settings.asr_service_id or "environment-asr", name="当前 ASR 接口",
        protocol=settings.remote_asr_protocol, base_url=settings.remote_asr_base_url,
        transcribe_path=settings.remote_asr_transcribe_path, health_path=settings.remote_asr_health_path,
        model_id=settings.remote_asr_model_id, model_version=settings.remote_asr_model_version,
        upstream_model=settings.remote_asr_upstream_model, api_key_env=settings.remote_asr_api_key_env,
        ca_file=str(settings.remote_asr_ca_file) if settings.remote_asr_ca_file else None,
        insecure_tls_origin=settings.remote_asr_insecure_tls_origin,
        connect_timeout_seconds=settings.remote_asr_connect_timeout_seconds,
        total_timeout_seconds=settings.remote_asr_total_timeout_seconds,
    )
