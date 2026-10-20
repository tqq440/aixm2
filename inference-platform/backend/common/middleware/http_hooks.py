from time import perf_counter
from uuid import uuid4

from flask import Flask, g, jsonify, request
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from backend.common.exceptions.app_error import AppError
from backend.schema.common.api_schema import ErrorDetail, ErrorResponse, RequestHeaders, ValidationIssue


def register_http_hooks(app: Flask) -> None:
    logger = app.extensions["app_logger"]

    @app.before_request
    def begin_request() -> None:
        g.request_id = uuid4().hex
        g.started_at = perf_counter()
        headers = RequestHeaders(request_id=request.headers.get("X-Request-ID"))
        if headers.request_id is not None:
            g.request_id = headers.request_id

    @app.after_request
    def finish_request(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["Cache-Control"] = "no-store"
        elapsed_ms = (perf_counter() - g.started_at) * 1000
        logger.info("%s %s %s %.2fms", request.method, request.path, response.status_code, elapsed_ms)
        return response

    def error_response(code: str, message: str, status: int, details=None):
        payload = ErrorResponse(
            error=ErrorDetail(code=code, message=message, details=details or []),
            request_id=g.request_id,
        )
        return jsonify(payload.model_dump(by_alias=True, mode="json")), status

    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        if exc.status_code >= 500:
            logger.error("%s", exc.code, exc_info=True)
        return error_response(exc.code, exc.message, exc.status_code)

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc: ValidationError):
        details = [ValidationIssue(field=".".join(map(str, item["loc"])), code=item["type"]) for item in exc.errors()]
        return error_response("VALIDATION_ERROR", "请求参数不符合要求。", 422, details)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        status = exc.code or 500
        messages = {400: "请求格式错误。", 404: "接口不存在。", 405: "该接口不支持此请求方法。", 413: "请求体过大。"}
        payload, _ = error_response(f"HTTP_{status}", messages.get(status, "HTTP 请求失败。"), status)
        # 使用原响应保留 Allow、Retry-After 等具有协议含义的头。
        response = exc.get_response()
        response.set_data(payload.get_data())
        response.content_type = "application/json"
        return response

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        logger.exception("未处理的请求异常")
        return error_response("INTERNAL_ERROR", "服务器内部错误，请提供请求编号以便排查。", 500)
