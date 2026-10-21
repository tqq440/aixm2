"""管理员节点视图只读缓存；显式刷新只读取 Node 就绪档案。"""

from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from backend.data.catalog.catalog_models import InferenceService,InferenceServiceRevision
from backend.data.database.database import database_now
from backend.data.gateway.key_models import GatewayNodeObservation
from backend.schema.host_node.execution_schema import ExecutionAvailability
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


class GatewayAdministration:
    def __init__(self,core,bridge):self.core,self.bridge=core,bridge

    def definitions(self,session):
        nodes={}
        for service in session.scalars(select(InferenceService)):
            if not service.active_revision:continue
            config=ServiceCatalog._definition(session.get(InferenceServiceRevision,service.active_revision))
            for target in config.targets:
                if not target.node:continue
                key=canonical_json_sha256(target.node.model_dump(mode="json",by_alias=True))
                nodes[key]=(target.node,config.service_id,target.target_id)
        return nodes

    def nodes(self):
        with self.core._transaction() as session:
            now=database_now(session);items=[]
            for key,(node,service,target) in self.definitions(session).items():
                observation=session.get(GatewayNodeObservation,key)
                items.append({"nodeId":node.node_id,"nodeServiceId":node.service_id,"serviceId":service,"targetId":target,
                    "configRevision":node.config_revision,"checkedAt":as_utc(observation.checked_at) if observation else None,
                    "stale":not observation or (now-observation.checked_at).total_seconds()>=15,
                    "snapshot":observation.snapshot if observation else None})
            return {"items":items,"notice":"Node就绪档案不代表模型实际设备；刷新不会调用推理。"}

    def _observe(self,node):
        try:
            status=ExecutionAvailability.model_validate(self.bridge.client_factory(node).availability())
            if (status.node_id,status.service_id,status.config_revision)!=(node.node_id,node.service_id,node.config_revision):
                return {"errorCode":"NODE_BINDING_MISMATCH"}
            return {"acceptingRequests":status.accepting_requests,"adapterConfigured":status.adapter_configured,
                "instanceId":str(status.instance_id) if status.instance_id else None,"kind":status.kind,"modelId":status.model_id,
                "issues":status.issues,"errorCode":None}
        except Exception:return {"errorCode":"NODE_UNAVAILABLE"}

    def refresh(self):
        with self.core._transaction() as session:definitions=self.definitions(session)
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix="gateway-node-status") as pool:
            jobs={key:pool.submit(self._observe,value[0]) for key,value in definitions.items()}
            results={key:job.result() for key,job in jobs.items()}
        with self.core._transaction(True) as session:
            for key,snapshot in results.items():
                row=session.get(GatewayNodeObservation,key)
                if row is None:session.add(GatewayNodeObservation(node_key=key,snapshot=snapshot,checked_at=database_now(session)))
                else:row.snapshot,row.checked_at=snapshot,database_now(session)
        return self.nodes()
