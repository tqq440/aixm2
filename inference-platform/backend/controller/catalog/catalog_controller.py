"""阶段一仅限本机管理；公共 API Key 身份系统在后续模块接入。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.common.middleware.local_configuration import authorize_local_configuration
from backend.schema.catalog.service_schema import (
    ActivateService, CatalogQuery, CheckTarget, SaveResourceGroup, SaveService, ServicePath, TargetPath,
)
from backend.schema.common.api_schema import ApiSchema, SuccessResponse


catalog_blueprint = Blueprint("service_catalog", __name__, url_prefix="/api/admin/service-catalog")


@catalog_blueprint.before_request
def guard():
    authorize_local_configuration(request)
    request.max_content_length = 128 * 1024
    if request.method != "GET":
        ApiSchema.model_validate(request.args.to_dict(flat=False))


def _service():
    return current_app.extensions["service_catalog"]


def _response(data):
    response = jsonify(SuccessResponse(data=data, request_id=g.request_id).model_dump(mode="json", by_alias=True))
    response.headers["Cache-Control"] = "no-store"
    return response


def _query():
    return CatalogQuery.model_validate({key: values[0] if len(values) == 1 else values
                                       for key, values in request.args.lists()})


@catalog_blueprint.get("/services")
def services():
    return _response(_service().list(_query()))


@catalog_blueprint.get("/services/<service_id>")
def detail(service_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    return _response(_service().get(ServicePath(service_id=service_id).service_id))


@catalog_blueprint.put("/services/<service_id>")
def save(service_id):
    return _response(_service().save(ServicePath(service_id=service_id).service_id,
                                     SaveService.model_validate(request.get_json())))


@catalog_blueprint.post("/services/<service_id>/activation")
def activate(service_id):
    return _response(_service().activate(ServicePath(service_id=service_id).service_id,
                                         ActivateService.model_validate(request.get_json())))


@catalog_blueprint.post("/services/<service_id>/targets/<target_id>/test")
def check(service_id, target_id):
    path = TargetPath(service_id=service_id, target_id=target_id)
    return _response(_service().check(path.service_id, path.target_id, CheckTarget.model_validate(request.get_json())))


@catalog_blueprint.get("/resource-groups")
def groups():
    return _response(_service().groups(_query()))


@catalog_blueprint.put("/resource-groups/<group_id>")
def save_group(group_id):
    return _response(_service().save_group(ServicePath(service_id=group_id).service_id,
                                           SaveResourceGroup.model_validate(request.get_json())))


@catalog_blueprint.post("/import-asr")
def import_asr():
    ApiSchema.model_validate(request.get_json())
    return _response(_service().import_asr())
