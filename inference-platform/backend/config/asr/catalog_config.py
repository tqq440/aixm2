"""验证登记连接但不访问网络，也不覆盖当前运行配置。"""

from backend.config.settings.app_settings import AppSettings, remote_asr_origin
from backend.schema.asr.catalog_schema import CatalogConnection


def catalog_settings(settings: AppSettings, connection: CatalogConnection) -> AppSettings:
    origin = remote_asr_origin(connection.base_url)
    if connection.ca_file is not None and connection.ca_file.startswith(("\\\\", "//")):
        raise ValueError("证书只允许本机文件，不读取UNC路径")
    values = settings.model_dump()
    values.update(remote_asr_base_url=origin, remote_asr_model_id=connection.model_id,
                  remote_asr_model_version=connection.model_version,
                  remote_asr_ca_file=connection.ca_file,
                  remote_asr_insecure_tls_origin=connection.insecure_tls_origin)
    return AppSettings.model_validate(values)
