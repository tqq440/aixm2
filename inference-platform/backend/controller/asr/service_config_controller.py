"""统一 ASR 接口配置；HTTP 层不执行音频推理、不改变任务状态。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.common.middleware.local_configuration import authorize_local_configuration
from backend.schema.asr.service_config_schema import (
    ServiceConfigDelete, ServiceConfigSave, ServiceConfigTest, ServiceSelection,
)
from backend.schema.common.api_schema import ApiSchema, SuccessResponse

asr_services_blueprint = Blueprint("asr_services", __name__, url_prefix="/api/asr/services")


@asr_services_blueprint.before_request
def validate_local_request():
    authorize_local_configuration(request)
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    request.max_content_length = 32 * 1024


def _service():
    return current_app.extensions["asr_service_configuration"]


def _response(data):
    return jsonify(SuccessResponse(data=data, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@asr_services_blueprint.get("")
def services():
    return _response(_service().state())


@asr_services_blueprint.put("/<service_id>")
def save(service_id):
    return _response(_service().save(service_id, ServiceConfigSave.model_validate(request.get_json())))


@asr_services_blueprint.delete("/<service_id>")
def delete(service_id):
    return _response(_service().delete(service_id, ServiceConfigDelete.model_validate(request.get_json())))


@asr_services_blueprint.post("/selection")
def select():
    return _response(_service().select(ServiceSelection.model_validate(request.get_json())))


@asr_services_blueprint.post("/test")
def test():
    command = ServiceConfigTest.model_validate(request.get_json())
    return _response(_service().test(command.config))
