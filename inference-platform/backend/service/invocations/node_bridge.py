"""4C1：中心公共许可到 Node 调用身份及可靠结果结算，不另建调度器。"""

from backend.common.exceptions.app_error import AppError
from backend.data.catalog.catalog_models import InferenceServiceRevision
from backend.data.database.database import database_now
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.schema.host_node.execution_schema import ExecutionAvailability, ExecutionCommand, ExecutionReceipt
from backend.schema.invocations.invocation_schema import InvocationLease
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.service.host_node.execution_client import NodeExecutionClient
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc
from datetime import datetime
from backend.schema.llm.online_schema import OnlineRequestInput
from backend.service.invocations.lease_heartbeat import LeaseHeartbeat


class NodeInvocationBridge:
    def __init__(self, core, *, client_factory=NodeExecutionClient, material_source=None):
        self.core, self.client_factory = core, client_factory
        self.material_source=material_source

    def _load(self, session, lease):
        row = self.core._lease(session, lease, live=False)
        if row is None:
            raise AppError("NODE_LEASE_MISMATCH", "调用身份与中心预约不匹配。", 409)
        definition = ServiceCatalog._definition(session.get(InferenceServiceRevision, row.service_revision))
        target = next(item for item in definition.targets if item.target_id == row.selected_target_id)
        if target.node is None:
            raise AppError("NODE_TARGET_NOT_BOUND", "目标尚未配置 Node 绑定，不能走 Node 执行。", 409)
        return row, definition, target

    def prepare(self, lease, manifest=None):
        lease = InvocationLease.model_validate(lease)
        with self.core._transaction() as session:
            row, definition, target = self._load(session, lease)
            existing = session.get(InferenceNodeDispatch, row.invocation_id)
            if existing:
                if manifest and existing.command_json.get("manifestHash")!=canonical_json_sha256(manifest.model_dump(mode="json",by_alias=True)):
                    raise AppError("NODE_INPUT_CONFLICT","已经固定的输入参数不匹配。",409)
                return ExecutionCommand.model_validate(existing.command_json)
            if row.state != "reserved" or self.core._lease(session, lease) is None:
                raise AppError("NODE_RESERVATION_NOT_LIVE", "仅能为有效、尚未发送的预约绑定 Node。", 409)
        # Node 可用性来自只读缓存，不触发业务 health 查询；不得持 SQL 锁通信。
        available = ExecutionAvailability.model_validate(self.client_factory(target.node).availability())
        if (available.node_id != target.node.node_id or available.service_id != target.node.service_id
                or available.config_revision != target.node.config_revision or available.kind != definition.kind
                or available.model_id != definition.model_id or available.instance_id is None):
            raise AppError("NODE_BINDING_MISMATCH", "Node、服务、模型或修订不匹配。", 409)
        if available.invocation_mode is not None and available.invocation_mode!=definition.invocation_mode:
            raise AppError("NODE_BINDING_MISMATCH","Node 调用模式与中心不可变修订不匹配。",409)
        if not available.accepting_requests or not available.adapter_configured or definition.protocol not in available.protocols:
            raise AppError("NODE_NOT_EXECUTION_READY", "Node 尚未就绪或业务适配未接通，未发送推理。", 503)
        with self.core._transaction(True) as session:
            row, definition, target = self._load(session, lease)
            existing = session.get(InferenceNodeDispatch, row.invocation_id)
            if existing:
                return ExecutionCommand.model_validate(existing.command_json)
            if row.state != "reserved" or self.core._lease(session, lease) is None:
                raise AppError("NODE_RESERVATION_NOT_LIVE", "可用性查询期间预约已变化，未派发。", 409)
            owner = self.core._owner(session, row)
            command = ExecutionCommand(node_id=target.node.node_id, service_id=target.node.service_id,
                config_revision=target.node.config_revision, instance_id=available.instance_id,
                invocation_id=row.invocation_id, attempt=row.attempt, service_revision=row.service_revision,
                target_id=row.selected_target_id, kind=row.kind, model_id=definition.model_id, protocol=definition.protocol,
                input_ref=owner.task_id if row.task_id else owner.online_request_id,
                input_sha256=owner.input_sha256 if row.task_id else owner.request_fingerprint, deadline_at=as_utc(row.deadline_at),
                manifest_hash=canonical_json_sha256(manifest.model_dump(mode="json",by_alias=True)) if manifest else None)
            payload = command.model_dump(mode="json", by_alias=True)
            now = database_now(session)
            session.add(InferenceNodeDispatch(invocation_id=row.invocation_id, command_json=payload,
                request_hash=canonical_json_sha256(payload), created_at=now, updated_at=now))
            self.core.repository.event(session, row, "invocation.node_bound", now,
                {"nodeId": command.node_id, "nodeServiceId": command.service_id,
                 "nodeConfigRevision": command.config_revision, "instanceId": str(command.instance_id)})
            return command

    def dispatch(self, lease):
        material=self.material_source.load(lease) if self.material_source else None
        command = self.prepare(lease, material.manifest if material else None)
        with self.core._transaction() as session:
            _, _, target = self._load(session, lease)
        if not self.core.mark_sent(lease):
            # 重复派发不能第二次 POST；恢复使用显式 fetch_result GET。
            return False
        try:
            with LeaseHeartbeat(self.core,lease):
                client=self.client_factory(target.node)
                receipt=client.execute_file(command,material) if material else client.execute(command)
            return self.apply_receipt(lease, receipt)
        except Exception:
            self.core.fail(lease)
            raise AppError("NODE_DISPATCH_UNCONFIRMED", "派发结果未知，保留容量；只能取回原调用结果，不能重做。", 502) from None

    def stream_online(self,lease,request):
        request=OnlineRequestInput.model_validate(request.model_dump())
        command=self.prepare(lease)
        if (canonical_json_sha256(request.model_dump(mode="json",by_alias=True))!=command.input_sha256
                or request.online_request_id!=command.input_ref):raise AppError("NODE_ONLINE_INPUT_CONFLICT","在线输入与受理指纹不符。",409)
        with self.core._transaction() as session:_,_,target=self._load(session,lease)
        if not self.core.mark_sent(lease):raise AppError("NODE_ONLINE_ALREADY_SENT","请求已经发送，不能重播生成。",409)
        def events():
            committed=False
            try:
                with LeaseHeartbeat(self.core,lease):
                    stream=self.client_factory(target.node).stream(command,request)
                    try:
                        for item in stream:
                            if item["event"]=="receipt":
                                committed=self.apply_receipt(lease,item["data"])
                                if not committed:self.core.fail(lease)
                                yield {"event":"done" if committed and item["data"].state=="succeeded" else "error",
                                    "data":self.snapshot(lease)}
                                return
                            yield item
                    finally:stream.close()
            except GeneratorExit:raise
            except Exception:
                self.core.fail(lease)
                yield {"event":"error","data":self.snapshot(lease)}
            finally:
                if not committed:self.core.fail(lease)
        return events()

    def fetch_result(self, lease):
        """维护者/后台明确核对时使用；公共查询 GET 不调用此方法。"""
        with self.core._transaction() as session:
            row, _, target = self._load(session, lease)
            if not session.get(InferenceNodeDispatch, row.invocation_id) or row.sent_at is None:
                raise AppError("NODE_DISPATCH_NOT_SENT", "调用尚未发送。", 409)
        return self.apply_receipt(lease, self.client_factory(target.node).result(lease.invocation_id))

    def apply_receipt(self, lease, receipt):
        receipt = ExecutionReceipt.model_validate(receipt.model_dump() if isinstance(receipt, ExecutionReceipt) else receipt)
        command = receipt.command.model_dump(mode="json", by_alias=True)
        if canonical_json_sha256(command) != receipt.request_hash:
            raise AppError("NODE_RECEIPT_MISMATCH", "回传身份指纹不匹配。", 409)
        result = receipt.result
        payload = result.model_dump(mode="json", by_alias=True) if result is not None else None
        if result is not None and canonical_json_sha256(payload) != receipt.result_hash:
            raise AppError("NODE_RESULT_MISMATCH", "回传结果指纹不匹配。", 409)
        # 忽略更新时间，重传相同终态/结果可幂等；未知/发送状态不构成可释放终态。
        terminal_hash = canonical_json_sha256({"state": receipt.state, "resultHash": receipt.result_hash})
        with self.core._transaction(True) as session:
            row, _, _ = self._load(session, lease)
            binding = session.get(InferenceNodeDispatch, row.invocation_id)
            if binding is None or binding.request_hash != receipt.request_hash or binding.command_json != command:
                raise AppError("NODE_RECEIPT_MISMATCH", "回传调用、次数、目标或实例与预约不符。", 409)
            if binding.terminal_hash:
                if receipt.state in {"sending","unknown"}:return True
                if binding.terminal_hash != terminal_hash:
                    raise AppError("NODE_TERMINAL_CONFLICT", "已确认终态冲突，保留原结果。", 409)
                return True
            if row.sent_at is None:
                raise AppError("NODE_RECEIPT_BEFORE_SEND", "调用尚未派发，拒绝回传。", 409)
            snapshot=receipt.monitoring.model_dump(mode="json",by_alias=True)
            previous=binding.monitoring_json or {};now=database_now(session)
            meaningful=("remoteState","progress","stage","queuePosition","actualDevice")
            changed=any(previous.get("snapshot",{}).get(k)!=snapshot.get(k) for k in meaningful)
            stamp=previous.get("progressEventAt")
            emit=not stamp or (as_utc(now)-as_utc(datetime.fromisoformat(stamp))).total_seconds()>=5 or receipt.state in {"succeeded","failed","cancelled"}
            if changed and emit:
                self.core.repository.event(session,row,"invocation.upstream_snapshot",now,
                    {k:snapshot.get(k) for k in meaningful},source="service")
                stamp=as_utc(now).isoformat()
            reason=snapshot.get("queryBudgetReason")
            if reason and reason!=previous.get("snapshot",{}).get("queryBudgetReason"):
                self.core.repository.event(session,row,"invocation.monitor_budget_wait",now,{"reason":reason})
            binding.monitoring_json={**previous,"snapshot":snapshot,"progressEventAt":stamp,
                "checkedAt":as_utc(now).isoformat(),"nodeQueryError":None,"executionError":receipt.error_code}
            if receipt.state not in {"succeeded", "failed", "cancelled"}:
                if binding.remote_state != receipt.state:
                    self.core.repository.event(session, row, "invocation.node_state", database_now(session), {"remoteState": receipt.state})
                binding.remote_state, binding.updated_at = receipt.state, database_now(session)
                return False
            owner = self.core._owner(session, row)
            if receipt.state == "succeeded" and result is None:
                if binding.remote_state != "succeeded":
                    self.core.repository.event(session, row, "invocation.node_result_pending", database_now(session))
                binding.remote_state, binding.updated_at = "succeeded", database_now(session)
                return False
            if receipt.state == "succeeded" and not self.core.result_matches(row, owner, result):
                raise AppError("NODE_RESULT_INVALID", "Node 终态缺少符合原任务约束的结果，容量保持。", 409)
            now = database_now(session)
            # 先记录已过期历史，再处理迟到终态；绝不静默把原超时改成成功。
            if row.state != "finished" and self.core._lease(session, lease) is None:
                self.core._finish(session, row, "unknown", False, now)
            if row.state == "finished":
                already_resolved = row.permit_state == "released" and row.resolved_outcome == receipt.state
                if not already_resolved and (row.permit_state != "uncertain" or row.resolved_outcome):
                    raise AppError("NODE_LATE_TERMINAL_CONFLICT", "原调用已经结算，拒绝覆盖。", 409)
                row.resolved_outcome, row.permit_state, row.updated_at = receipt.state, "released", now
                if row.online_request_id:
                    owner.permit_state = "released"
                binding.late_result_json = payload
                self.core.repository.event(session, row, "invocation.node_late_result_saved", now,
                    {"remoteOutcome": receipt.state, "resultSaved": payload is not None})
                if not already_resolved:
                    self.core.repository.event(session, row, "invocation.capacity_released", now)
            elif receipt.state == "succeeded":
                if not self.core.complete_in_transaction(session, lease, result, payload, receipt.result_hash):
                    raise AppError("NODE_RESULT_NOT_COMMITTED", "结果未能保存，容量保持。", 409)
            else:
                self.core._finish(session, row, receipt.state, True, now)
            binding.terminal_hash, binding.remote_state, binding.updated_at = terminal_hash, receipt.state, now
            self.core.repository.event(session, row, "invocation.node_result_committed", now,
                {"nodeId": receipt.command.node_id, "remoteOutcome": receipt.state, "resultSaved": payload is not None,"errorCode":receipt.error_code})
            return True

    def snapshot(self, lease):
        """纯本地 GET 数据，不连接 Node，不返回业务正文。"""
        with self.core._transaction() as session:
            row, _, target = self._load(session, lease)
            binding = session.get(InferenceNodeDispatch, row.invocation_id)
            return {"invocationId": row.invocation_id, "node": binding.command_json if binding else None,
                "remoteState": binding.remote_state if binding else None,
                "terminalCommitted": bool(binding and binding.terminal_hash),
                "resultSaved": bool(binding and binding.terminal_hash and binding.remote_state == "succeeded"),
                "resultPending": bool(binding and (binding.remote_state == "succeeded" or (binding.monitoring_json or {}).get("snapshot",{}).get("remoteState")=="succeeded") and not binding.terminal_hash),
                "lateResultSaved": bool(binding and binding.late_result_json is not None),
                "assignedDevice":{"kind":target.device_kind,"deviceId":target.device_id},
                "monitoring":(binding.monitoring_json or {}).get("snapshot",{}) if binding else {},
                "nodeQueryError":(binding.monitoring_json or {}).get("nodeQueryError") if binding else None,
                "executionError":(binding.monitoring_json or {}).get("executionError") if binding else None,
                "platform": self.core._view(row)}

    def monitoring(self,invocation_id):
        from backend.data.invocations.invocation_models import InferenceInvocation
        from backend.service.invocations.node_collector import NodeResultCollector
        with self.core._transaction() as session:
            row=session.get(InferenceInvocation,invocation_id)
            if row is None:raise AppError("INVOCATION_NOT_FOUND","未找到调用。",404)
            if row.sent_at is None:
                return {"invocationId":invocation_id,"platform":self.core._view(row),"monitoring":{},"assignedDevice":None,
                    "resultSaved":False,"resultPending":False,"notice":"尚未派发，进度和实际设备未知。"}
            lease=NodeResultCollector.lease(row)
        return self.snapshot(lease)

    def reconcile(self,invocation_id,*,cancel=False):
        from backend.data.invocations.invocation_models import InferenceInvocation
        from backend.service.invocations.node_collector import NodeResultCollector
        with self.core._transaction(True) as session:
            row=session.get(InferenceInvocation,invocation_id)
            if row is None:raise AppError("INVOCATION_NOT_FOUND","未找到调用。",404)
            if cancel:self.core.cancel_in_transaction(session,row)
            if row.sent_at is None:
                if cancel:return self.core._view(row)
                raise AppError("NODE_DISPATCH_NOT_SENT","尚未派发，不核对业务。",409)
            lease=NodeResultCollector.lease(row)
            _,_,target=self._load(session,lease)
        client=self.client_factory(target.node)
        receipt=client.cancel(invocation_id) if cancel else client.reconcile(invocation_id)
        self.apply_receipt(lease,receipt)
        return self.snapshot(lease)
