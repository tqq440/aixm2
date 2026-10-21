"""中心读取 Node 缓存并保存结果；不把页面 GET 变成上游查询。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from threading import Lock

from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.schema.invocations.invocation_schema import InvocationLease
from backend.utils.time.time_utils import as_utc


class NodeResultCollector:
    def __init__(self,bridge):
        self.bridge=bridge;self.pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix="node-result")
        self.futures={};self.busy_services=set();self.guard=Lock()

    @staticmethod
    def lease(row):
        return InvocationLease(invocation_id=row.invocation_id,worker_id=row.worker_id,lease_token=row.lease_token,
            attempt=row.attempt,service_revision=row.service_revision,target_id=row.selected_target_id,deadline_at=row.deadline_at)

    def tick(self):
        for identifier,(future,service) in list(self.futures.items()):
            if future.done():
                self.futures.pop(identifier);self.busy_services.discard(service)
                try:future.result()
                except Exception:pass  # 错误快照在 _fetch 保存；不输出网络/正文异常。
        if len(self.futures)>=4:return
        with self.bridge.core._transaction() as session:
            now=database_now(session);due=[]
            for binding,row in session.execute(select(InferenceNodeDispatch,InferenceInvocation).join(InferenceInvocation,
                    InferenceInvocation.invocation_id==InferenceNodeDispatch.invocation_id).where(
                    InferenceNodeDispatch.terminal_hash.is_(None),InferenceInvocation.sent_at.is_not(None)).order_by(InferenceNodeDispatch.updated_at)):
                m=binding.monitoring_json or {};stamp=m.get("nextFetchAt")
                if row.deadline_at<=now or (stamp and now<as_utc(datetime.fromisoformat(stamp)).replace(tzinfo=None)):continue
                service=binding.command_json["nodeId"]+":"+binding.command_json["serviceId"]
                due.append((self.lease(row),service))
        for lease,service in due:
            if len(self.futures)>=4:break
            if lease.invocation_id in self.futures or service in self.busy_services:continue
            self.busy_services.add(service);self.futures[lease.invocation_id]=(self.pool.submit(self._fetch,lease),service)

    def _fetch(self,lease):
        error=None
        try:
            self.bridge.core.renew(lease)
            self.bridge.fetch_result(lease)
        except Exception as exc:error=exc.code if isinstance(exc,AppError) else "NODE_SNAPSHOT_UNAVAILABLE"
        with self.bridge.core._transaction(True) as session:
            row=self.bridge.core._lease(session,lease,live=False);binding=session.get(InferenceNodeDispatch,lease.invocation_id)
            if row is None or binding is None:return
            now=database_now(session);m=dict(binding.monitoring_json or {})
            if binding.terminal_hash:return
            failures=min(m.get("fetchFailures",0)+1,20) if error else 0
            delay=min(5*(2**max(failures-1,0)),60)
            if error and m.get("nodeQueryError")!=error:
                self.bridge.core.repository.event(session,row,"invocation.node_monitor_failed",now,{"errorCode":error})
            binding.monitoring_json={**m,"nodeQueryError":error,"fetchFailures":failures,
                "nextFetchAt":as_utc(now+timedelta(seconds=delay)).isoformat()}

    def close(self):self.pool.shutdown(wait=True,cancel_futures=True)
