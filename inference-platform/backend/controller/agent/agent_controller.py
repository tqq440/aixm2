"""固定本机实例查询及令牌保护的启停入口。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.schema.agent.agent_schema import ManagementCommand
from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import InstancePath

agent_blueprint = Blueprint('agent', __name__, url_prefix='/api/instances')


def _query(schema):
    return schema.model_validate({key: values[0] if len(values) == 1 else values for key, values in request.args.lists()})


def _response(value):
    return jsonify(SuccessResponse(data=value, request_id=g.request_id).model_dump(mode='json', by_alias=True))


@agent_blueprint.get('/<instance_id>/management')
def status(instance_id):
    _query(ApiSchema)
    return _response(current_app.extensions['agent_service'].status(InstancePath(instance_id=instance_id).instance_id))


@agent_blueprint.get('/<instance_id>/management/sessions')
def sessions(instance_id):
    return _response(current_app.extensions['agent_service'].sessions(
        InstancePath(instance_id=instance_id).instance_id, _query(RegistryListQuery)))


@agent_blueprint.post('/<instance_id>/management/start')
def start(instance_id):
    return _command(instance_id, 'start')


@agent_blueprint.post('/<instance_id>/management/stop')
def stop(instance_id):
    return _command(instance_id, 'stop')


def _command(instance_id, action):
    service = current_app.extensions['agent_service']
    service.authorize_http(request.remote_addr, request.headers.get('X-Management-Token'))
    _query(ApiSchema)
    path = InstancePath(instance_id=instance_id)
    command = ManagementCommand.model_validate(request.get_json())
    return _response(service.command(path.instance_id, action, command)), 202
