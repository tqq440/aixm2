"""显式中心 Node 执行器：原队列/额度、就绪观测、调用及结果采集。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from time import monotonic

from sqlalchemy import select

from backend.data.catalog.catalog_models import InferenceServiceRevision
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.schema.catalog.service_schema import ServiceDefinition
from backend.schema.host_node.execution_schema import ExecutionAvailability
from backend.service.invocations.node_collector import NodeResultCollector
from backend.utils.json.json_utils import canonical_json_sha256


class NodeExecutorRunner:
    def __init__(self,queue,bridge,*,workers=4,worker_id="node-executor"):
        if not 1<=workers<=8:raise ValueError("执行槽为 1..8")
        self.queue,self.bridge,self.workers,self.worker_id=queue,bridge,workers,worker_id
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix="node-execute")
        self.probes=ThreadPoolExecutor(max_workers=4,thread_name_prefix="node-ready")
        self.collector=NodeResultCollector(bridge);self.futures=[];self.observations={};self.last_probe=0

    def _observe(self,node):
        try:
            status=ExecutionAvailability.model_validate(self.bridge.client_factory(node).availability()).model_dump(mode="json",by_alias=True)
            return {**status,"checkedAt":datetime.now(timezone.utc)}
        except Exception:return None

    def refresh(self):
        if monotonic()-self.last_probe<5:return
        self.last_probe=monotonic()
        with self.bridge.core._transaction() as session:
            definitions=list(session.scalars(select(InferenceServiceRevision.config).join(InferenceInvocation,
                InferenceInvocation.service_revision==InferenceServiceRevision.revision).where(InferenceInvocation.state=="waiting")))
        nodes={}
        for raw in definitions:
            for target in ServiceDefinition.model_validate(raw).targets:
                if target.enabled and target.node:nodes[canonical_json_sha256(target.node.model_dump(mode="json",by_alias=True))]=target.node
        jobs={key:self.probes.submit(self._observe,node) for key,node in nodes.items()}
        self.observations={key:job.result() for key,job in jobs.items() if job.result() is not None}

    def _execute(self,lease):
        try:return self.bridge.dispatch(lease)
        except Exception:
            self.bridge.core.fail(lease)
            return False

    def tick(self):
        self.futures=[f for f in self.futures if not f.done()]
        self.bridge.core.recover();self.collector.tick()
        if self.queue.settings.common_queue_backend=="redis":self.queue.tick()
        self.refresh()
        slots=self.workers-len(self.futures)
        if slots:
            for lease in self.bridge.core.claim_round(self.worker_id,limit=min(slots,20),require_node=True,node_observations=self.observations):
                self.futures.append(self.pool.submit(self._execute,lease))

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True);self.probes.shutdown(wait=True,cancel_futures=True);self.collector.close()
