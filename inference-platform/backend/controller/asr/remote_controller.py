"""外部服务只读状态入口；无远端启停或任意地址代理。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.service.asr.target_status import TargetStatusService
from backend.schema.asr.catalog_schema import CatalogQuery
from backend.service.asr.catalog_service import AsrCatalogService
from backend.service.asr.scheduling_service import AsrSchedulingService

remote_asr_blueprint = Blueprint("remote_asr", __name__, url_prefix="/api/asr/external")


@remote_asr_blueprint.get("/status")
def get_status():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    status = current_app.extensions["remote_asr_service"].status()
    return jsonify(SuccessResponse(data=status, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@remote_asr_blueprint.get("/capacity")
def get_capacity():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    remote = current_app.extensions["remote_asr_service"]
    status = TargetStatusService(remote.database, remote.settings).status()
    return jsonify(SuccessResponse(data=status, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@remote_asr_blueprint.get("/targets")
def get_targets():
    values = {key: values[0] if len(values) == 1 else values
              for key, values in request.args.lists()}
    query = CatalogQuery.model_validate(values)
    service = AsrCatalogService(current_app.extensions["database"], current_app.config["APP_SETTINGS"])
    page = service.list_targets(query)
    return jsonify(SuccessResponse(data=page, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@remote_asr_blueprint.get("/scheduling")
def get_scheduling():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    service = AsrSchedulingService(current_app.extensions["database"], current_app.config["APP_SETTINGS"])
    status = service.status()
    return jsonify(SuccessResponse(data=status, request_id=g.request_id).model_dump(mode="json", by_alias=True))
