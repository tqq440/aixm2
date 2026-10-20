from flask import Blueprint, current_app, g, jsonify, request

from backend.schema.common.api_schema import SuccessResponse
from backend.schema.health.health_schema import HealthQuery, HealthStatus
from backend.service.health.health_service import HealthService

health_blueprint = Blueprint("health", __name__, url_prefix="/api")


@health_blueprint.get("/health")
def get_health():
    HealthQuery.model_validate(request.args.to_dict(flat=False))
    service: HealthService = current_app.extensions["health_service"]
    result = SuccessResponse[HealthStatus](data=service.check(), request_id=g.request_id)
    return jsonify(result.model_dump(by_alias=True, mode="json"))
