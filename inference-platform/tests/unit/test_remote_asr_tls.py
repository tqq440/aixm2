"""仅在回环地址验证真实 TLS 握手；不调用课题组服务或模型。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading

import pytest

from backend.config.settings.app_settings import AppSettings
from backend.service.asr.remote_v2_adapter import probe_remote_asr


@pytest.fixture
def isolated_self_signed_health(tmp_path):
    openssl = Path(sys.prefix) / "Library/bin/openssl.exe"
    if not openssl.is_file():
        pytest.skip("现有环境没有生成隔离测试证书的 OpenSSL；不安装新依赖")
    certificate = tmp_path / "test-ca.pem"
    key = tmp_path / "test-key.pem"
    config = tmp_path / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
    subprocess.run([str(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1", "-config", str(config)],
                   capture_output=True, check=True, timeout=15,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            observed.append(self.path)
            body = json.dumps({"success": True, "data": {"status": "ok", "model_loaded": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", certificate, observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_untrusted_certificate_requires_explicit_matching_exception_and_is_not_globally_trusted(isolated_self_signed_health):
    origin, certificate, observed = isolated_self_signed_health
    values = {"remote_asr_base_url": origin, "remote_asr_model_id": "isolated-tls-fixture",
              "remote_asr_model_version": "fixture-deployment-1"}
    assert not probe_remote_asr(AppSettings(**values)).ready
    assert observed == []
    assert probe_remote_asr(AppSettings(**values, remote_asr_insecure_tls_origin=origin)).ready
    assert observed == ["/api/v1/asr/health"]
    assert not probe_remote_asr(AppSettings(**values)).ready
    assert observed == ["/api/v1/asr/health"]
    assert probe_remote_asr(AppSettings(**values, remote_asr_ca_file=certificate)).ready
    assert observed == ["/api/v1/asr/health", "/api/v1/asr/health"]
