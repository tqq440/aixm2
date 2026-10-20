from flask import Blueprint, current_app, g, jsonify, request

from backend.schema.common.api_schema import ApiSchema, SuccessResponse

runtime_blueprint = Blueprint("runtime", __name__, url_prefix="/api")


@runtime_blueprint.get("/runtime")
def get_runtime():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    status = current_app.extensions["runtime_service"].get_status()
    return jsonify(SuccessResponse(data=status, request_id=g.request_id).model_dump(by_alias=True, mode="json"))
