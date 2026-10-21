"""统一受理与只读查询；原任务、调用、幂等和 outbox 在同一事务中保存。"""

from datetime import timedelta
import json
from time import monotonic, sleep
from uuid import uuid4
from sqlalchemy import select, func
from backend.common.exceptions.app_error import AppError
from backend.data.catalog.catalog_models import InferenceService, InferenceServiceRevision
from backend.data.database.database import database_now
from backend.data.files.upload_store import LocalUploadStore, UploadTooLargeError, EmptyUploadError
from backend.data.invocations.invocation_models import InferenceApplicationBudget, InferenceInvocation
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.tasks.task_models import TaskRecord, IdempotencyRecord
from backend.schema.invocations.invocation_schema import InvocationBinding
from backend.schema.llm.online_schema import OnlineRequestInput
from backend.schema.gateway.gateway_schema import PublicRequestView
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.service.invocations.node_collector import NodeResultCollector
from backend.utils.audio.audio_utils import decode_audio_file
from backend.utils.image.image_utils import decode_image_file
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


class GatewayRequests:
    def __init__(self, core, bridge):self.core, self.bridge = core, bridge

    @staticmethod
    def application(session, identifier):
        app = session.get(InferenceApplicationBudget, identifier)
        if not app or not app.enabled:raise AppError("APPLICATION_DISABLED", "应用已停用。",403)
        return app

    def definition(self, session, application, service_id, kind):
        app = self.application(session, application)
        if service_id not in app.allowed_services:raise AppError("APPLICATION_SERVICE_FORBIDDEN", "应用没有此服务权限。",403)
        row = session.get(InferenceService, service_id)
        if not row or not row.enabled or not row.active_revision:raise AppError("SERVICE_UNAVAILABLE", "服务未启用。",503)
        definition = ServiceCatalog._definition(session.get(InferenceServiceRevision, row.active_revision))
        if definition.kind != kind:raise AppError("SERVICE_KIND_MISMATCH", "请求与服务类型不符。",422)
        if not any(t.enabled and t.node for t in definition.targets):raise AppError("SERVICE_NODE_REQUIRED", "该服务尚未关联目标 Node。",503)
        return definition

    def services(self, application):
        with self.core._transaction() as session:
            app = self.application(session, application);items=[]
            for row in session.scalars(select(InferenceService).where(InferenceService.enabled.is_(True)).order_by(InferenceService.service_id)):
                if row.service_id not in app.allowed_services or not row.active_revision:continue
                d=ServiceCatalog._definition(session.get(InferenceServiceRevision,row.active_revision))
                items.append({"serviceId":d.service_id,"name":d.name,"kind":d.kind,"modelId":d.model_id,
                    "modelVersion":d.model_version,"invocationMode":d.invocation_mode,"maxFileBytes":d.max_file_bytes,
                    "maxAudioSeconds":d.max_audio_seconds,"configured":any(t.enabled and t.node for t in d.targets)})
            return {"items":items}

    def submit_file(self, application, kind, command, idempotency_key, stream):
        with self.core._transaction() as session:d=self.definition(session,application,command.service_id,kind)
        if kind=="asr" and any(t.enabled and t.node for t in d.targets) and d.protocol=="remote-v2" and command.language is not None:
            raise AppError("LANGUAGE_UNSUPPORTED","此 ASR 协议不支持指定语言，请省略 language。",422)
        store=LocalUploadStore(self.core.settings.upload_dir,media_type="audio" if kind=="asr" else "image")
        parameters={"language":command.language} if kind=="asr" else command.parameters.model_dump()
        with store.lock():
            stored=None;keep=False
            try:
                stored=store.save(stream,max_bytes=min(d.max_file_bytes,20*1024*1024 if kind=="asr" else 10*1024*1024))
                try:
                    media=decode_audio_file(stored.path,max_samples=min(d.max_audio_seconds,300)*16000) if kind=="asr" else decode_image_file(stored.path,max_file_bytes=10*1024*1024,max_pixels=20_000_000)
                except Exception:raise AppError("INVALID_MEDIA","媒体无效、超时长或超出图片尺寸限制。",422) from None
                fingerprint=canonical_json_sha256({"kind":kind,"serviceId":command.service_id,"parameters":parameters,"sha256":stored.sha256})
                with self.core._transaction(True) as session:
                    old=session.scalar(select(IdempotencyRecord).where(IdempotencyRecord.caller_id==application,IdempotencyRecord.idempotency_key==idempotency_key))
                    if old:
                        if old.request_fingerprint!=fingerprint:raise AppError("IDEMPOTENCY_CONFLICT","相同幂等键携带不同请求。",409)
                        return self._view(session,self._owned(session,application,old.task_id,"task"))
                    current=self.definition(session,application,command.service_id,kind)
                    if current!=d:raise AppError("SERVICE_REVISION_CHANGED","服务配置在上传期间变化，请用原幂等键重新提交。",409)
                    now=database_now(session);identifier=uuid4().hex
                    owner=TaskRecord(task_id=identifier,caller_id=application,task_type=kind,model_id=d.model_id,
                        model_version=d.model_version or "unreported",parameters=parameters,input_ref=stored.input_ref,
                        input_filename="audio.upload" if kind=="asr" else "image.upload",input_sha256=stored.sha256,
                        input_size_bytes=stored.size_bytes,input_media_type="audio" if kind=="asr" else "image",
                        audio_duration_seconds=media.duration_seconds if kind=="asr" else None,
                        image_width=media.width if kind=="vision" else None,image_height=media.height if kind=="vision" else None,
                        status="queued",attempt=0,max_attempts=1,execution_plan_version=1,dispatch_version=1,
                        queue_key="common."+canonical_json_sha256({"service":d.service_id,"model":d.model_id}),
                        queue_deadline_at=now+timedelta(seconds=self.core.settings.queue_timeout_seconds),
                        total_deadline_at=now+timedelta(seconds=self.core.settings.total_timeout_seconds),created_at=now,updated_at=now)
                    session.add(owner);session.flush()
                    row=self.core.bind_in_transaction(session,InvocationBinding(application_id=application,service_id=d.service_id,owner_id=identifier,owner_kind="task"))
                    session.add(IdempotencyRecord(id=uuid4().hex,caller_id=application,idempotency_key=idempotency_key,request_fingerprint=fingerprint,task_id=identifier,created_at=now))
                    view=self._view(session,row)
                keep=True
                return view
            except UploadTooLargeError:raise AppError("UPLOAD_TOO_LARGE","上传超出服务限制。",413) from None
            except EmptyUploadError:raise AppError("UPLOAD_EMPTY","上传文件为空。",422) from None
            finally:
                if stored and not keep:store.delete(stored.input_ref)

    def _owned(self,session,application,identifier,kind):
        column=InferenceInvocation.task_id if kind=="task" else InferenceInvocation.online_request_id
        row=session.scalar(select(InferenceInvocation).where(column==identifier))
        app=self.application(session,application)
        if not row or row.application_id!=application or row.service_id not in app.allowed_services:
            raise AppError("REQUEST_NOT_FOUND","未找到当前应用可访问的请求。",404)
        return row

    def _view(self,session,row):
        owner=self.core._owner(session,row)
        dispatch=session.get(InferenceNodeDispatch,row.invocation_id)
        value={**self.core._view(row),"id":row.task_id or row.online_request_id,"status":self.status(row),
            "modelId":owner.model_id,"modelVersion":owner.model_version,"resultAvailable":bool(row.task_id and (owner.result_json is not None or (dispatch and dispatch.late_result_json is not None))),
            "error":owner.error_json,"cancelRequested":owner.status=="cancel_requested" if row.task_id else owner.cancel_requested,
            "input":{"sizeBytes":owner.input_size_bytes,"durationSeconds":owner.audio_duration_seconds,"imageWidth":owner.image_width,"imageHeight":owner.image_height} if row.task_id else None,
            "outputCharacters":owner.output_characters if row.online_request_id else None,
            "finishReason":owner.finish_reason if row.online_request_id else None}
        return PublicRequestView.model_validate(value).model_dump(mode="json",by_alias=True)

    @staticmethod
    def status(row):
        if row.permit_state=="uncertain":return "unknown"
        if row.state=="finished":return row.outcome if row.outcome in {"succeeded","cancelled"} else "failed"
        return "waiting" if row.state=="waiting" else "running"

    def get(self,application,identifier,kind="task"):
        with self.core._transaction() as session:return self._view(session,self._owned(session,application,identifier,kind))

    def listing(self,application,query,*,admin=False):
        with self.core._transaction() as session:
            statement=select(InferenceInvocation)
            if not admin:
                app=self.application(session,application)
                statement=statement.where(InferenceInvocation.application_id==application,InferenceInvocation.service_id.in_(app.allowed_services),InferenceInvocation.task_id.is_not(None))
            if query.service_id:statement=statement.where(InferenceInvocation.service_id==query.service_id)
            if query.kind:statement=statement.where(InferenceInvocation.kind==query.kind)
            if query.status=="unknown":statement=statement.where(InferenceInvocation.permit_state=="uncertain")
            elif query.status=="waiting":statement=statement.where(InferenceInvocation.state=="waiting")
            elif query.status=="running":statement=statement.where(InferenceInvocation.state.in_(("reserved","sent")))
            elif query.status:statement=statement.where(InferenceInvocation.outcome==query.status,InferenceInvocation.permit_state!="uncertain")
            total=session.scalar(select(func.count()).select_from(statement.subquery()))
            rows=session.scalars(statement.order_by(InferenceInvocation.accepted_at.desc(),InferenceInvocation.invocation_id.desc()).offset((query.page-1)*query.page_size).limit(query.page_size))
            return {"items":[self._view(session,row) for row in rows],"page":query.page,"pageSize":query.page_size,"total":total}

    def monitoring(self,application,identifier,kind="task"):
        with self.core._transaction() as session:invocation=self._owned(session,application,identifier,kind).invocation_id
        return self.clean_monitor(self.bridge.monitoring(invocation))

    @staticmethod
    def clean_monitor(value):
        return {k:value.get(k) for k in ("invocationId","platform","remoteState","resultSaved","resultPending","lateResultSaved","assignedDevice","monitoring","nodeQueryError","executionError","notice")}

    def result(self,application,identifier):
        with self.core._transaction() as session:
            row=self._owned(session,application,identifier,"task");owner=self.core._owner(session,row)
            dispatch=session.get(InferenceNodeDispatch,row.invocation_id)
            late=dispatch.late_result_json if dispatch else None
            if owner.result_json is None and late is None:raise AppError("RESULT_NOT_READY","平台尚未保存结果。",409)
            return {"taskId":identifier,"result":owner.result_json if owner.result_json is not None else late,"lateResult":owner.result_json is None,"originalOutcome":row.outcome}

    def events(self,application,identifier,query,kind="task"):
        self.get(application,identifier,kind)
        return self.core.events(kind,identifier,query,application_id=application)

    def cancel(self,application,identifier,kind="task"):
        with self.core._transaction(True) as session:
            row=self._owned(session,application,identifier,kind)
            action=self.core.cancel_in_transaction(session,row);invocation=row.invocation_id;sent=row.sent_at is not None
        error=None
        if sent and action=="cancel_requested":
            try:self.bridge.reconcile(invocation,cancel=True)
            except AppError as exc:error=exc.code
        return {**self.get(application,identifier,kind),"cancelAction":action,"cancelError":error}

    def online(self,application,command,key):
        identifier=canonical_json_sha256({"application":application,"key":key,"kind":"llm"})[:32]
        with self.core._transaction() as session:
            old=session.get(OnlineRequestRecord,identifier)
            if old:
                row=self._owned(session,application,identifier,"online")
                if row.service_id!=command.service_id:raise AppError("IDEMPOTENCY_CONFLICT","幂等键已用于其他服务。",409)
                model_id,version=old.model_id,old.model_version
            else:
                d=self.definition(session,application,command.service_id,"llm");model_id,version=d.model_id,d.model_version or "unreported"
        request=OnlineRequestInput(online_request_id=identifier,model_id=model_id,model_version=version,messages=command.messages,parameters=command.parameters)
        accepted=self.core.accept_online(application,command.service_id,request)
        end=monotonic()+self.core.settings.llm_admission_wait_seconds
        lease=None
        while True:
            lease=self.core.claim_online(accepted["invocationId"],accepted["workerId"],require_node=True)
            if lease or monotonic()>=end:break
            current=self.core.get(accepted["invocationId"])
            if current["state"]!="waiting":return identifier,None,self.get(application,identifier,"online")
            sleep(min(.05,max(0,end-monotonic())))
        if not lease:
            with self.core._transaction(True) as session:
                row=self._owned(session,application,identifier,"online")
                if row.state=="waiting":self.core._finish(session,row,"busy",True,database_now(session))
            raise AppError("LLM_BUSY",f"服务繁忙，未发送。请求编号：{identifier}。",429)
        try:events=self.bridge.stream_online(lease,request)
        except Exception:
            self.core.fail(lease)
            raise AppError("LLM_NODE_UNAVAILABLE",f"Node 不可用，请查询请求 {identifier}，不要更换幂等键重试。",503) from None
        return identifier,events,None

    def disconnect_online(self,application,identifier):
        # 清理不依赖当前权限：Key/服务权限可能在连接期间被撤销。
        with self.core._transaction() as session:
            row=session.scalar(select(InferenceInvocation).where(InferenceInvocation.online_request_id==identifier,InferenceInvocation.application_id==application))
            if row is None or row.state=="finished":return
            lease=NodeResultCollector.lease(row)
        self.core.fail(lease)
