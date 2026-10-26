"""统一后台查询：全局四个、每服务一个；浏览器 GET 只读快照。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Lock
from uuid import uuid4

from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.common.http.bounded_client import ProtocolFailure
from backend.data.host_node.execution_models import HostNodeExecution
from backend.schema.host_node.execution_schema import AdapterOutcome, ExecutionCommand
from backend.schema.host_node.upstream_schema import ExecutionManifest
from backend.service.host_node.execution_service import TERMINAL
from backend.service.host_node.monitor_budget_gate import MonitorBudgetGate
from backend.utils.time.iso_time import iso_utc
from backend.utils.time.time_utils import as_utc


class NodeMonitorService:
    def __init__(self,executions, *, budget_client):
        self.executions=executions;self.guard=Lock();self.busy={};self.futures=[];self.closed=False
        self.budget=MonitorBudgetGate(executions,budget_client)
        self.budget.recover()
        self.pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix="node-query")

    def _enter(self,identifier,service):
        with self.guard:
            if self.closed or len(self.busy)>=4 or any(v[0]==service for v in self.busy.values()) or identifier in self.busy:return None
            token=uuid4().hex;self.busy[identifier]=(service,token);return token

    def _leave(self,identifier,token):
        with self.guard:
            if self.busy.get(identifier,(None,None))[1]==token:self.busy.pop(identifier,None)

    def _service_key(self,session,row):
        config=self.executions.configuration(session,row)
        query=(config.get("upstream") or {}).get("query",{})
        return (config.get("internalBaseUrl") or row.service_id)+":"+(query.get("statusPath") or row.service_id)

    def tick(self):
        adapter=self.executions.adapter
        if not getattr(adapter,"requires_material",False):return
        self.futures=[f for f in self.futures if not f.done()]
        for identifier in self.budget.outstanding():
            token=self._enter(identifier,"budget-release:"+identifier)
            if token:self.futures.append(self.pool.submit(self._release_budget,identifier,token))
        with self.executions._transaction() as session:
            now=self.executions.node._now(session);due=[]
            for row in session.scalars(select(HostNodeExecution).where(HostNodeExecution.state.in_(("sending","unknown"))).order_by(HostNodeExecution.updated_at)):
                m=row.monitoring_json or {}
                if not m.get("supported") or m.get("pollStopped") or m.get("budgetPhase")=="uncertain":continue
                stamp=m.get("nextPollAt")
                if stamp and now<as_utc(datetime.fromisoformat(stamp)).replace(tzinfo=None):continue
                due.append((row.invocation_id,self._service_key(session,row)))
        for identifier,service in due:
            token=self._enter(identifier,service)
            if token:self.futures.append(self.pool.submit(self._poll,identifier,token,False))

    def reconcile(self,identifier):
        view=self.executions.get(identifier)
        if view["state"] in TERMINAL:return self.executions.get(identifier,include_result=True)
        with self.executions._transaction() as session:
            key=self._service_key(session,session.get(HostNodeExecution,identifier))
        token=self._enter(identifier,key)
        if not token:raise AppError("NODE_MONITOR_BUSY","该服务已有查询或全局查询已满。",409)
        return self._poll(identifier,token,True)

    def _release_budget(self,identifier,token):
        try:self.budget.release(identifier)
        finally:self._leave(identifier,token)

    def confirm_query_stopped(self,identifier):
        with self.executions._transaction() as session:
            key=self._service_key(session,session.get(HostNodeExecution,identifier))
        token=self._enter(identifier,key)
        if not token:raise AppError("NODE_MONITOR_BUSY","本机查询尚在运行，不能归还。",409)
        try:return self.budget.confirm_stopped(identifier)
        finally:self._leave(identifier,token)

    def _poll(self,identifier,token,manual):
        granted=False
        try:
            with self.executions._transaction(immediate=True) as session:
                self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
                if row is None or row.state in TERMINAL:return self.executions._view(row,include_result=True) if row else None
                command=ExecutionCommand.model_validate(row.command_json);config=self.executions.configuration(session,row)
                now=self.executions.node._now(session);m=dict(row.monitoring_json or {})
                if not manual and as_utc(command.deadline_at).replace(tzinfo=None)<=now:
                    row.state,row.error_code,row.updated_at="unknown","EXECUTION_DEADLINE_EXPIRED",now
                    row.monitoring_json={**m,"pollStopped":"deadline","pollToken":None}
                    self.executions._event(session,row,"EXECUTION_MONITOR_DEADLINE")
                    return self.executions._view(row,include_result=True)
                job_id=m.get("jobId")
            if self.budget.acquire(identifier) is None:
                return self.executions.get(identifier,include_result=True)
            granted=True
            with self.executions._transaction(immediate=True) as session:
                self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
                if row.state in TERMINAL:return self.executions._view(row,include_result=True)
                row.monitoring_json={**(row.monitoring_json or {}),"pollToken":token,"lastQueryAt":iso_utc(self.executions.node._now(session))}
            adapter=self.executions.adapter
            if not getattr(adapter,"requires_material",False):raise ProtocolFailure("UPSTREAM_QUERY_UNSUPPORTED")
            status=adapter.query(command,config,job_id)
            if not self._apply(identifier,token,status):return self.executions.get(identifier,include_result=True)
            if status.state=="succeeded" and status.result is None and config["upstream"]["query"].get("resultPath"):
                status=adapter.query(command,config,job_id,result=True)
                if status.state!="succeeded":raise ProtocolFailure("UPSTREAM_RESULT_STATE_INVALID")
                if not self._apply(identifier,token,status):return self.executions.get(identifier,include_result=True)
            if status.state in TERMINAL and (status.state!="succeeded" or status.result is not None):
                self.executions.commit(identifier,AdapterOutcome(state=status.state,result=status.result,
                    error_code="UPSTREAM_REPORTED_FAILED" if status.state=="failed" else None),source="query",poll_token=token)
            self._schedule(identifier,token,config)
        except Exception as error:
            self._error(identifier,token,error)
        finally:
            try:
                if granted:self.budget.release(identifier,confirmed=True)
            finally:self._leave(identifier,token)
        return self.executions.get(identifier,include_result=True)

    def _apply(self,identifier,token,status):
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
            m=dict(row.monitoring_json or {})
            if row.state in TERMINAL or m.get("pollToken")!=token:return False
            if status.result is not None and status.state!="succeeded":raise ProtocolFailure("UPSTREAM_RESULT_STATE_INVALID")
            if status.result is not None and m.get("manifest"):
                from backend.service.host_node.http_adapter import NodeHttpAdapter
                NodeHttpAdapter.validate_result(status.result,ExecutionCommand.model_validate(row.command_json),ExecutionManifest.model_validate(m["manifest"]))
            now=self.executions.node._now(session)
            fields={"remoteState":status.state,"progress":status.progress,"stage":status.stage,
                "queuePosition":status.queue_position,"actualDevice":status.actual_device.model_dump(mode="json",by_alias=True) if status.actual_device else None}
            changed=any(m.get(k)!=v for k,v in fields.items())
            previous=m.get("lastEventAt")
            allow_event=not previous or (now-as_utc(datetime.fromisoformat(previous)).replace(tzinfo=None)).total_seconds()>=5
            if changed and (allow_event or status.state in TERMINAL):
                row.updated_at=now;self.executions._event(session,row,"UPSTREAM_PROGRESS_REPORTED",details=fields,source="service")
                m["lastEventAt"]=iso_utc(now)
            row.monitoring_json={**m,**fields,"jobId":status.job_id,"lastTrustedAt":iso_utc(now),
                "lastError":None,"failures":0,"stateSource":"query"}
            return True

    def _schedule(self,identifier,token,config):
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier);m=row.monitoring_json or {}
            if row.state not in TERMINAL and m.get("pollToken")==token:
                now=self.executions.node._now(session)
                row.monitoring_json={**m,"pollToken":None,"nextPollAt":iso_utc(now+timedelta(seconds=config["upstream"]["query"]["intervalSeconds"]))}

    def _error(self,identifier,token,error):
        code=error.code if isinstance(error,ProtocolFailure) else "UPSTREAM_QUERY_INVALID"
        retry=error.retry_after if isinstance(error,ProtocolFailure) else None
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier);m=dict(row.monitoring_json or {})
            if row.state in TERMINAL or m.get("pollToken")!=token:return
            now=self.executions.node._now(session);failures=min(m.get("failures",0)+1,20)
            config=self.executions.configuration(session,row)
            interval=config.get("upstream",{}).get("query",{}).get("intervalSeconds",5)
            delay=max(min(interval*(2**(failures-1)),60),retry or 0)
            row.updated_at=now
            if m.get("lastError")!=code:self.executions._event(session,row,"UPSTREAM_MONITOR_FAILED",details={"errorCode":code})
            row.monitoring_json={**m,"lastError":code,"lastErrorAt":iso_utc(now),"failures":failures,
                "pollToken":None,"nextPollAt":iso_utc(now+timedelta(seconds=delay))}

    def cancel(self,identifier):
        view=self.executions.get(identifier)
        if view["state"] in TERMINAL:return view
        with self.executions._transaction() as session:
            key=self._service_key(session,session.get(HostNodeExecution,identifier))
        token=self._enter(identifier,key)
        if not token:raise AppError("NODE_MONITOR_BUSY","服务正在查询，请稍后取消。",409)
        try:
            with self.executions._transaction(immediate=True) as session:
                self.executions.node._require_owner(session)
                row=session.get(HostNodeExecution,identifier);config=self.executions.configuration(session,row);job_id=(row.monitoring_json or {}).get("jobId")
                if not row.monitoring_json.get("cancelRequested"):
                    row.monitoring_json={**row.monitoring_json,"cancelRequested":True};row.updated_at=self.executions.node._now(session)
                    self.executions._event(session,row,"UPSTREAM_CANCEL_REQUESTED")
            self.executions.adapter.cancel(ExecutionCommand.model_validate(view["command"]),config,job_id)
        except Exception:
            with self.executions._transaction(immediate=True) as session:
                self.executions.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
                self.executions._event(session,row,"UPSTREAM_CANCEL_UNCONFIRMED")
            raise AppError("NODE_CANCEL_UNCONFIRMED","取消未获得可靠应答，未确认远端停止。",502) from None
        finally:self._leave(identifier,token)
        return self.executions.get(identifier)

    def close(self):
        with self.guard:self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
