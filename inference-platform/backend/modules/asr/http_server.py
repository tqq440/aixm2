"""用户手动启动的回环 Whisper HTTP 服务；平台不会自动启动此入口。"""

import argparse
import logging

from flask import Flask, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from backend.common.cli.execution import execute
from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.controller.asr.http_controller import AsrHttpRequest, asr_http_blueprint
from backend.schema.asr.http_schema import AsrHttpOptions
from backend.schema.common.api_schema import ApiSchema
from backend.service.asr.http_service import AsrHttpService


def create_app(settings: AppSettings, *, adapter=None) -> Flask:
    """仅构造应用；必须显式 prepare_model 后才能转写，便于隔离测试。"""
    app = Flask(__name__)
    app.request_class = AsrHttpRequest
    app.config.update(
        MAX_CONTENT_LENGTH=settings.asr_max_file_bytes + 65_536,
        MAX_FORM_MEMORY_SIZE=65_536,
        MAX_FORM_PARTS=8,
    )
    app.extensions["asr_http_service"] = AsrHttpService(settings, adapter=adapter)
    app.register_blueprint(asr_http_blueprint)

    @app.errorhandler(AppError)
    def app_error(error):
        return jsonify(error={"message": error.message, "type": "asr_error", "code": error.code}), error.status_code

    @app.errorhandler(ValidationError)
    def validation_error(_error):
        return jsonify(error={"message": "请求字段不符合支持的音频接口契约。",
                              "type": "invalid_request_error", "code": "VALIDATION_ERROR"}), 422

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={"message": "HTTP 请求无效或请求体超过上限。",
                              "type": "invalid_request_error", "code": f"HTTP_{error.code}"}), error.code

    @app.errorhandler(Exception)
    def unknown_error(_error):
        logging.getLogger(__name__).exception("Whisper HTTP 请求失败")
        return jsonify(error={"message": "转写服务失败，请核对服务日志。",
                              "type": "asr_error", "code": "ASR_SERVICE_FAILED"}), 500

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="手动运行已有 Whisper 的 HTTP 服务；只监听 127.0.0.1，不下载模型。")
    parser.add_argument("--port", type=int, default=8001, help="回环监听端口，默认 8001")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        options = AsrHttpOptions.model_validate(vars(args))
        app = create_app(settings)
        app.extensions["asr_http_service"].prepare_model()
        logger.info("Whisper HTTP 已加载；监听 127.0.0.1:%s，单请求槽", options.port)
        app.run(host="127.0.0.1", port=options.port, debug=False, use_reloader=False, threaded=True)
        return ApiSchema()

    return execute(operation, log_filename="asr-http.log", command_name="Whisper HTTP")


if __name__ == "__main__":
    raise SystemExit(main())
