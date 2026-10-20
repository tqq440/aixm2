"""HTTP 层只解析输入、调用服务并封装响应。"""

from flask import Blueprint, current_app, g, jsonify, request, Response

from backend.common.exceptions.app_error import AppError
from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.tasks.task_schema import (AsrTaskForm, ImageAccessQuery, ModelListQuery, SubmissionHeaders,
                                            TaskListQuery, TaskPath, UploadMetadata, VisionTaskForm)
from backend.service.tasks.task_service import TaskService

task_blueprint = Blueprint("tasks", __name__, url_prefix="/api")


def _single_values(multidict):
    # 重复字段保留为列表交由 Pydantic 拒绝，不能静默使用第一项。
    return {key: values[0] if len(values) == 1 else values for key, values in multidict.lists()}


def _response(data, status=200):
    body = SuccessResponse(data=data, request_id=g.request_id)
    return jsonify(body.model_dump(by_alias=True, mode="json")), status


@task_blueprint.get("/models")
def get_models():
    query = ModelListQuery.model_validate(_single_values(request.args))
    return _response(current_app.extensions["model_service"].list_models(query.task_type))


@task_blueprint.post("/tasks/asr")
def create_asr_task():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    headers = SubmissionHeaders(idempotency_key=request.headers.get("Idempotency-Key"))
    if request.mimetype != "multipart/form-data":
        raise AppError("MULTIPART_REQUIRED", "请使用 multipart/form-data 上传 file 字段。", 415)
    form = AsrTaskForm.model_validate(_single_values(request.form))
    if set(request.files) != {"file"} or len(request.files.getlist("file")) != 1:
        raise AppError("UPLOAD_FILE_REQUIRED", "必须且只能上传一个名为 file 的音频文件。", 422)
    upload = request.files["file"]
    metadata = UploadMetadata(file_name=upload.filename)
    service: TaskService = current_app.extensions["task_service"]
    receipt = service.submit(form, headers, metadata, upload.stream)
    response, status = _response(receipt, 200 if receipt.replayed else 201)
    response.headers["Location"] = f"/api/tasks/{receipt.task.task_id}"
    return response, status


@task_blueprint.post("/tasks/vision")
def create_vision_task():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    headers = SubmissionHeaders(idempotency_key=request.headers.get("Idempotency-Key"))
    if request.mimetype != "multipart/form-data":
        raise AppError("MULTIPART_REQUIRED", "请使用 multipart/form-data 上传 file 字段。", 415)
    form = VisionTaskForm.model_validate(_single_values(request.form))
    if set(request.files) != {"file"} or len(request.files.getlist("file")) != 1:
        raise AppError("UPLOAD_FILE_REQUIRED", "必须且只能上传一个名为 file 的图片文件。", 422)
    upload = request.files["file"]
    metadata = UploadMetadata(file_name=upload.filename)
    receipt = current_app.extensions["task_service"].submit_vision(form, headers, metadata, upload.stream)
    response, status = _response(receipt, 200 if receipt.replayed else 201)
    response.headers["Location"] = f"/api/tasks/{receipt.task.task_id}"
    return response, status


@task_blueprint.get("/tasks/<task_id>/image")
def get_task_image(task_id):
    path = TaskPath(task_id=task_id)
    query = ImageAccessQuery.model_validate(_single_values(request.args))
    data = current_app.extensions["task_service"].get_image(path.task_id, annotated=query.variant == "annotated")
    response = Response(data, mimetype="image/png")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = f'inline; filename="{path.task_id}-{query.variant}.png"'
    return response


@task_blueprint.get("/tasks")
def get_tasks():
    query = TaskListQuery.model_validate(_single_values(request.args))
    return _response(current_app.extensions["task_service"].list_tasks(query))


@task_blueprint.get("/tasks/<task_id>")
def get_task(task_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    path = TaskPath(task_id=task_id)
    return _response(current_app.extensions["task_service"].get_task(path.task_id))


@task_blueprint.get("/tasks/<task_id>/result")
def get_task_result(task_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    path = TaskPath(task_id=task_id)
    return _response(current_app.extensions["task_service"].get_result(path.task_id))


@task_blueprint.post("/tasks/<task_id>/cancel")
def cancel_task(task_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    path = TaskPath(task_id=task_id)
    # 取消只接受路径中的任务编号，避免静默忽略错误字段。
    if request.get_data():
        if not request.is_json:
            raise AppError("JSON_REQUIRED", "取消请求无需请求体，或提交空 JSON 对象。", 415)
        ApiSchema.model_validate(request.get_json())
    return _response(current_app.extensions["task_service"].cancel(path.task_id))
