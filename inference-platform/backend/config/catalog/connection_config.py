"""只在显式健康测试时读取凭据和证书；导入模块无副作用。"""

import os
import ssl

from backend.common.exceptions.app_error import ConfigurationError


def connection_security(target):
    secret = os.environ.get(target.api_key_env) if target.api_key_env else None
    if target.api_key_env and (not secret or not secret.strip() or len(secret) > 4096
                              or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
        raise ConfigurationError("服务凭据环境变量未设置或无效")
    if target.insecure_tls_origin:
        if target.ca_file or target.insecure_tls_origin != target.base_url:
            raise ConfigurationError("TLS 例外与目标不匹配")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    else:
        context = ssl.create_default_context(cafile=target.ca_file)
    return context, {"Authorization": f"Bearer {secret}"} if secret else {}
