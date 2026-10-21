"""独立 Whisper HTTP 路由；由专用入口注册，不挂载到平台 API。"""

from tempfile import TemporaryFile

from flask import Blueprint, Request, current_app, jsonify, request

from backend.common.exceptions.app_error import AppError
from backend.schema.asr.http_schema import AsrHttpTranscription
from backend.schema.common.api_schema import ApiSchema


asr_http_blueprint = Blueprint("asr_http", __name__)


class AsrHttpRequest(Request):
    """multipart 解析阶段也使用专用临时目录，由 Request.close 关闭并删除。"""

    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        root = current_app.extensions["asr_http_service"].upload_root
        root.mkdir(parents=True, exist_ok=True)
        return TemporaryFile(mode="w+b", dir=root, prefix="multipart-")


@asr_http_blueprint.get("/health")
def health():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    payload = current_app.extensions["asr_http_service"].health()
    return jsonify(payload), 200 if payload["model_loaded"] else 503


@asr_http_blueprint.get("/v1/models")
def models():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    return jsonify(current_app.extensions["asr_http_service"].models())


@asr_http_blueprint.post("/v1/audio/transcriptions")
def transcribe():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    service = current_app.extensions["asr_http_service"]
    # 在 multipart 解析及任何文件暂存之前争取执行槽，忙时立即拒绝。
    with service.request_slot():
        if request.mimetype != "multipart/form-data":
            raise AppError("INVALID_CONTENT_TYPE", "请使用 multipart/form-data 上传音频。", 415)
        form = {key: values[0] if len(values) == 1 else values
                for key, values in request.form.lists()}
        command = AsrHttpTranscription.model_validate(form)
        if set(request.files) != {"file"} or len(request.files.getlist("file")) != 1:
            raise AppError("AUDIO_FILE_REQUIRED", "必须且只能通过 file 字段上传一个音频文件。", 422)
        return jsonify(service.transcribe(request.files["file"].stream, command))
