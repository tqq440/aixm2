"""Node 调用身份、协议执行和原子结果保存；没有推理任务队列。"""

import json
from contextlib import contextmanager

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.data.host_node.execution_models import HostNodeExecution
from backend.data.host_node.node_models import HostNodeInstance, HostNodeService, HostNodeServiceRevision
from backend.data.host_node.node_repository import NodeRepository
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.host_node.execution_schema import AdapterOutcome, ExecutionCommand, InvocationId
from backend.schema.host_node.upstream_schema import RemoteDetectionResult
from backend.schema.invocations.invocation_schema import OnlineCompletion
from backend.schema.vision.detection_schema import DetectionResult
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.iso_time import iso_utc
from backend.utils.time.time_utils import as_utc


TERMINAL = {"succeeded", "failed", "cancelled"}


class NodeExecutionService:
    def __init__(self, node, *, adapter=None):
        self.node, self.adapter = node, adapter

    @contextmanager
    def _transaction(self, *, immediate=False):
        try:
            with self.node.database.transaction(immediate=immediate) as session:
                yield session
        except SQLAlchemyError:
            raise AppError("NODE_EXECUTION_STORAGE_UNAVAILABLE", "执行资料暂不可用；不重做推理。", 503) from None

    def authorize(self, header):
        if not self.node.started or not self.node._matches(header, self.node.settings.executor_token):
            raise AppError("NODE_EXECUTOR_UNAUTHORIZED", "需要独立中心执行凭据。", 401)

    def recover(self):
        with self._transaction(immediate=True) as session:
            self.node._require_owner(session)
            for row in session.scalars(select(HostNodeExecution).where(HostNodeExecution.state.in_(("sending","unknown")))):
                if row.state=="sending":
                    row.state, row.error_code, row.updated_at = "unknown", "NODE_RESTARTED_DURING_CALL", self.node._now(session)
                    self._event(session,row,"EXECUTION_RECOVERED_UNKNOWN")
                row.monitoring_json={**(row.monitoring_json or {}),"pollToken":None}

    def availability(self, service_id):
        with self._transaction() as session:
            row=self.node._service(session,service_id)
            instance=session.get(HostNodeInstance,row.current_instance_id) if row.current_instance_id else None
            snapshot=self.node._snapshot(row,instance,self.node._now(session))
            real=bool(getattr(self.adapter,"requires_material",False))
            issues=self.adapter.issues(row.config_json) if real else ([] if self.adapter else ["EXECUTION_ADAPTER_NOT_CONFIGURED"])
            protocols=([row.config_json["upstream"]["protocol"]] if real and not issues else
                       ([] if real else list(self.adapter.protocols) if self.adapter else []))
            return {"nodeId":self.node.settings.node_id,"serviceId":row.service_id,"configRevision":row.config_revision,
                "instanceId":row.current_instance_id,"kind":row.config_json["kind"],"modelId":row.config_json["modelId"],
                "acceptingRequests":snapshot["acceptingRequests"] and not issues,"adapterConfigured":bool(self.adapter) and not issues,
                "inputRequired":real,"invocationMode":(row.config_json.get("upstream") or {}).get("mode") if real else None,
                "protocols":protocols,"issues":snapshot["issues"]+issues}

    @staticmethod
    def monitoring(row):
        value=row.monitoring_json or {}
        names=("supported","remoteState","progress","stage","queuePosition","actualDevice","lastQueryAt",
               "lastTrustedAt","lastError","lastErrorAt","pollStopped","cancelRequested","stateSource")
        return {key:value.get(key) for key in names}

    @staticmethod
    def _view(row, *, include_result=False):
        return {"command":row.command_json,"requestHash":row.request_hash,"state":row.state,"resultHash":row.result_hash,
            "result":row.result_json if include_result else None,"errorCode":row.error_code,
            "createdAt":iso_utc(row.created_at),"updatedAt":iso_utc(row.updated_at),
            "monitoring":NodeExecutionService.monitoring(row)}

    @staticmethod
    def _event(session,row,event,*,details=None,source="node"):
        NodeRepository.event(session,row.updated_at,event,source=source,
            service=session.get(HostNodeService,row.service_id),instance=session.get(HostNodeInstance,row.instance_id),
            details={"invocationId":row.invocation_id,"serviceId":row.service_id,"instanceId":row.instance_id,
                "attempt":row.command_json["attempt"],"serviceRevision":row.command_json["serviceRevision"],
                "targetId":row.command_json["targetId"],"state":row.state,"errorCode":row.error_code,**(details or {})})

    def get(self, invocation_id, *, include_result=False):
        identifier=TypeAdapter(InvocationId).validate_python(invocation_id)
        with self._transaction() as session:
            row=session.get(HostNodeExecution,identifier)
            if row is None:raise AppError("NODE_EXECUTION_NOT_FOUND","调用未登记，404 不能作为停止证明。",404)
            return self._view(row,include_result=include_result)

    @staticmethod
    def configuration(session,row):
        revision=session.scalar(select(HostNodeServiceRevision).where(
            HostNodeServiceRevision.service_id==row.service_id,
            HostNodeServiceRevision.revision==row.command_json["configRevision"]))
        if revision is None:raise AppError("NODE_CONFIG_MISSING","执行修订缺失。",503)
        return revision.config_json

    def _begin(self,command,material=None):
        payload=command.model_dump(mode="json",by_alias=True);digest=canonical_json_sha256(payload)
        with self._transaction() as session:
            existing=session.get(HostNodeExecution,command.invocation_id)
            if existing:
                if existing.request_hash!=digest:raise AppError("NODE_EXECUTION_CONFLICT","相同调用编号携带不同执行身份，拒绝重做。",409)
                return self._view(existing,include_result=True),None
            if not self.adapter or command.protocol not in self.adapter.protocols:
                raise AppError("NODE_EXECUTION_ADAPTER_MISSING","尚未配置业务适配器。",503)
            config=dict(self.node._service(session,command.service_id).config_json)
        real=bool(getattr(self.adapter,"requires_material",False))
        preflight_error=None
        if real:
            if self.adapter.issues(config):raise AppError("NODE_PROTOCOL_INCOMPLETE","业务接入配置尚未闭环。",503)
            if config["upstream"]["protocol"]!=command.protocol:
                raise AppError("NODE_PROTOCOL_MISMATCH","执行协议与本机登记不符。",409)
            if material is None:raise AppError("NODE_INPUT_REQUIRED","此服务需要经过校验的实际输入。",422)
            try:self.adapter.preflight(config)
            except Exception:preflight_error="NODE_UPSTREAM_CONFIG_INVALID_NOT_SENT"
            if not preflight_error:
                try:material.validate(command,config["upstream"],self.node.settings.database_path.parent/(self.node.settings.database_path.name+".inputs"))
                except AppError:preflight_error="NODE_INPUT_INVALID_NOT_SENT"
        with self._transaction(immediate=True) as session:
            self.node._require_owner(session)
            existing=session.get(HostNodeExecution,command.invocation_id)
            if existing:
                if existing.request_hash!=digest:raise AppError("NODE_EXECUTION_CONFLICT","相同调用编号携带不同执行身份，拒绝重做。",409)
                return self._view(existing,include_result=True),None
            row=self.node._service(session,command.service_id)
            instance=session.get(HostNodeInstance,row.current_instance_id) if row.current_instance_id else None
            now=self.node._now(session)
            if (command.node_id!=self.node.settings.node_id or command.config_revision!=row.config_revision
                    or str(command.instance_id)!=row.current_instance_id or command.kind!=row.config_json["kind"]
                    or command.model_id!=row.config_json["modelId"]):
                raise AppError("NODE_EXECUTION_IDENTITY_STALE","Node、模型、修订或启动实例已变化。",409)
            if not self.node._snapshot(row,instance,now)["acceptingRequests"]:
                if real:preflight_error="NODE_GATE_CLOSED_NOT_SENT"
                else:raise AppError("NODE_EXECUTION_GATE_CLOSED","服务未就绪或门禁关闭。",409)
            if as_utc(command.deadline_at).replace(tzinfo=None)<=now:
                if real:preflight_error="NODE_DEADLINE_NOT_SENT"
                else:raise AppError("NODE_EXECUTION_EXPIRED","执行期限已过，未发送。",409)
            query=config.get("upstream",{}).get("query",{})
            monitor={"supported":query.get("protocol","none")!="none","progress":None,"actualDevice":None,
                "remoteState":None,"lastError":None,"pollToken":None}
            if material and material.manifest:monitor["manifest"]=material.manifest.model_dump(mode="json",by_alias=True)
            record=HostNodeExecution(invocation_id=command.invocation_id,service_id=row.service_id,
                instance_id=str(command.instance_id),request_hash=digest,command_json=payload,node_epoch_id=self.node.epoch_id,
                state="failed" if preflight_error else "sending",error_code=preflight_error,monitoring_json=monitor,created_at=now,updated_at=now)
            session.add(record)
            if preflight_error:
                self._event(session,record,"EXECUTION_REJECTED_NOT_SENT")
                return self._view(record,include_result=True),None
            instance.may_have_accepted=True;self._event(session,record,"EXECUTION_SEND_STARTED")
        return None,config

    def unknown(self,identifier):
        with self._transaction(immediate=True) as session:
            self.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
            if row.state=="sending":
                row.state,row.error_code,row.updated_at="unknown","UPSTREAM_OUTCOME_UNCONFIRMED",self.node._now(session)
                self._event(session,row,"EXECUTION_UNKNOWN")
            return self._view(row,include_result=True)

    def commit(self,identifier,outcome,*,source="response",poll_token=None):
        if not isinstance(outcome,AdapterOutcome):raise ValueError("需要可信类型化结果")
        outcome=AdapterOutcome.model_validate(outcome.model_dump())
        result=outcome.result.model_dump(mode="json",by_alias=True) if outcome.result else None
        if len(json.dumps(result,ensure_ascii=False).encode("utf-8"))>2*1024*1024:raise ValueError("结果过大")
        fingerprint=canonical_json_sha256(result) if result is not None else None
        with self._transaction(immediate=True) as session:
            self.node._require_owner(session);row=session.get(HostNodeExecution,identifier)
            if poll_token and (row.monitoring_json or {}).get("pollToken")!=poll_token:return self._view(row,include_result=True)
            if row.state in TERMINAL:
                if (row.state,row.result_hash)!=(outcome.state,fingerprint):
                    self._event(session,row,"EXECUTION_TERMINAL_CONFLICT")
                return self._view(row,include_result=True)
            command=ExecutionCommand.model_validate(row.command_json)
            if outcome.result and not isinstance(outcome.result,{"asr":TranscriptionResult,
                    "vision":(DetectionResult,RemoteDetectionResult),"llm":OnlineCompletion}[command.kind]):
                raise ValueError("结果类型与任务不匹配")
            if outcome.state=="pending":
                if row.monitoring_json.get("jobId") not in (None,outcome.job_id):
                    raise ValueError("业务任务编号冲突")
                row.monitoring_json={**row.monitoring_json,"jobId":outcome.job_id}
                row.updated_at=self.node._now(session);self._event(session,row,"UPSTREAM_JOB_ACCEPTED")
                return self._view(row,include_result=True)
            if outcome.result and row.monitoring_json.get("manifest"):
                from backend.schema.host_node.upstream_schema import ExecutionManifest
                from backend.service.host_node.http_adapter import NodeHttpAdapter
                NodeHttpAdapter.validate_result(outcome.result,command,ExecutionManifest.model_validate(row.monitoring_json["manifest"]))
            row.state,row.result_json,row.result_hash=outcome.state,result,fingerprint
            row.error_code,row.updated_at=outcome.error_code,self.node._now(session)
            row.monitoring_json={**row.monitoring_json,"remoteState":outcome.state,"lastTrustedAt":iso_utc(row.updated_at),
                "stateSource":source,"pollToken":None}
            self._event(session,row,"EXECUTION_RESULT_SAVED" if result is not None else "EXECUTION_TERMINAL_CONFIRMED",source="service")
            return self._view(row,include_result=True)

    def execute(self,command,material=None):
        command=ExecutionCommand.model_validate(command.model_dump() if isinstance(command,ExecutionCommand) else command)
        existing,config=self._begin(command,material)
        if existing:return existing
        try:
            if getattr(self.adapter,"requires_material",False):
                if config["upstream"]["mode"]=="stream":raise ValueError("在线请求须使用流式入口")
                outcome=self.adapter.execute(command,material,config)
            else:outcome=self.adapter.execute(command)
            return self.commit(command.invocation_id,outcome)
        except SQLAlchemyError:
            raise AppError("NODE_EXECUTION_STORAGE_UNAVAILABLE","结果未确认保存，不重做推理。",503) from None
        except AppError:raise
        except Exception:return self.unknown(command.invocation_id)

    def stream(self,command,material):
        command=ExecutionCommand.model_validate(command)
        if command.kind!="llm":raise AppError("NODE_STREAM_KIND_INVALID","流式入口仅支持 LLM。",422)
        existing,config=self._begin(command,material)
        if not getattr(self.adapter,"requires_material",False):
            raise AppError("NODE_STREAM_NOT_CONFIGURED","未配置流适配器。",503)
        def generate():
            committed=False
            try:
                if existing:
                    yield {"event":"receipt","data":existing};return
                count=0
                for chunk in self.adapter.stream(command,material,config):
                    if chunk.kind=="delta":
                        count+=len(chunk.text)
                        yield {"event":"delta","data":{"invocationId":command.invocation_id,"text":chunk.text}}
                    elif chunk.kind=="done":
                        if chunk.finish_reason in ("stop","length"):
                            outcome=AdapterOutcome(state="succeeded",result=OnlineCompletion(output_characters=count,finish_reason=chunk.finish_reason))
                        else:outcome=AdapterOutcome(state="failed",error_code="LLM_CONTENT_FILTERED")
                        receipt=self.commit(command.invocation_id,outcome);committed=True
                        yield {"event":"receipt","data":receipt};return
                raise ValueError("缺少结束信号")
            except GeneratorExit:raise
            except Exception:
                receipt=self.unknown(command.invocation_id)
                yield {"event":"receipt","data":receipt}
            finally:
                if not existing and not committed:self.unknown(command.invocation_id)
                material.online=None
        return generate()
