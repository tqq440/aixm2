"""节点查询只读；本阶段维护操作只经本机命令调用 Service。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import InstanceListQuery, InstancePath, NodePath

node_blueprint = Blueprint("nodes", __name__, url_prefix="/api")


def _query(schema):
    values = {key: items[0] if len(items) == 1 else items for key, items in request.args.lists()}
    return schema.model_validate(values)


def _response(value):
    return jsonify(SuccessResponse(data=value, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@node_blueprint.get("/nodes")
def list_nodes():
    return _response(current_app.extensions["node_registry_service"].list_nodes(_query(RegistryListQuery)))


@node_blueprint.get("/nodes/<node_id>")
def get_node(node_id):
    _query(ApiSchema)
    path = NodePath(node_id=node_id)
    return _response(current_app.extensions["node_registry_service"].get_node(path.node_id))


@node_blueprint.get("/instances")
def list_instances():
    return _response(current_app.extensions["node_registry_service"].list_instances(_query(InstanceListQuery)))


@node_blueprint.get("/instances/<instance_id>")
def get_instance(instance_id):
    _query(ApiSchema)
    path = InstancePath(instance_id=instance_id)
    return _response(current_app.extensions["node_registry_service"].get_instance(path.instance_id))


@node_blueprint.get("/instances/<instance_id>/sessions")
def list_sessions(instance_id):
    path = InstancePath(instance_id=instance_id)
    return _response(current_app.extensions["node_registry_service"].list_sessions(
        path.instance_id, _query(RegistryListQuery)))
