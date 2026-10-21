"""三类实际 HTTP 协议适配，地址与凭据只来自已登记的本机服务。"""

import json
import ssl
from time import monotonic
from datetime import datetime, timezone

from backend.common.http.bounded_client import ProtocolFailure, request_bytes
from backend.schema.asr.asr_schema import TranscriptionResult, TranscriptionSegment, AsrEngineMetadata
from backend.schema.asr.remote_v2_schema import RemoteTranscriptResponse, OpenAiAudioTranscript
from backend.schema.host_node.execution_schema import AdapterOutcome
from backend.schema.host_node.job_schema import JobIdentity, JobStatus, JobCancellation
from backend.schema.host_node.upstream_schema import RemoteDetectionResult
from backend.service.llm.engine_adapter import HttpChatAdapter
from backend.utils.stream.sse_utils import decode_sse_json_object
from backend.utils.time.time_utils import as_utc


class NodeHttpAdapter:
    protocols=("remote-v2","openai-audio","vision-json-v1","chat-completions-sse")
    requires_material=True

    def __init__(self,settings,*,transport=None):self.settings,self.transport=settings,transport

    def issues(self,config):
        upstream=config.get("upstream")
        if not upstream:return ["BUSINESS_PROTOCOL_NOT_CONFIGURED"]
        issues=[]
        if not config.get("internalBaseUrl"):issues.append("BUSINESS_ADDRESS_MISSING")
        if upstream.get("tokenEnv") and upstream["tokenEnv"] not in self.settings.upstream_tokens:issues.append("UPSTREAM_CREDENTIAL_MISSING")
        if upstream["mode"]=="async" and (upstream["query"]["protocol"]=="none" or not (upstream["query"]["resultPath"] or upstream["query"].get("resultInStatus"))):issues.append("COMPLETION_UNAVAILABLE")
        return issues

    def _token(self,config):
        reference=config["upstream"].get("tokenEnv")
        return self.settings.upstream_tokens[reference].get_secret_value() if reference else None

    def preflight(self,config):
        self._token(config)
        ssl.create_default_context(cafile=config["upstream"].get("caFile"))

    @staticmethod
    def match(identity,command,job_id=None):
        if any(getattr(identity,key)!=getattr(command,key) for key in ("invocation_id","attempt","service_revision","config_revision")):
            raise ProtocolFailure("UPSTREAM_CORRELATION_MISMATCH")
        if job_id and identity.job_id!=job_id:raise ProtocolFailure("UPSTREAM_JOB_MISMATCH")

    def _request(self,config,method,path,*,timeout,maximum=2*1024*1024,**kwargs):
        upstream=config["upstream"]
        return request_bytes(method,config["internalBaseUrl"]+path,token=self._token(config),ca_file=upstream.get("caFile"),
            timeout=timeout,connect_timeout=upstream["connectTimeoutSeconds"],transport=self.transport,maximum=maximum,**kwargs)

    def execute(self,command,material,config):
        u=config["upstream"];manifest=material.manifest
        remaining=(as_utc(command.deadline_at)-datetime.now(timezone.utc)).total_seconds()
        if remaining<=0:raise ProtocolFailure("UPSTREAM_DEADLINE_EXPIRED")
        data={"request_id":command.invocation_id}
        if u["protocol"]=="remote-v2":
            data.update(outputs="transcript",engine="sensevoice");field="audio"
        elif u["protocol"]=="openai-audio":
            data.update(model=u["upstreamModel"],response_format="json");field="file"
            if manifest.language:data["language"]=manifest.language
        else:
            field="image";data.update(model=u["upstreamModel"],parameters=json.dumps(manifest.model_dump(mode="json",by_alias=True)))
        if u["mode"]=="async" or u["query"]["protocol"]!="none":
            data["context"]=json.dumps(command.model_dump(mode="json",by_alias=True))
        reply=self._request(config,"POST",u["invokePath"],timeout=min(remaining,u["timeoutSeconds"]),
            data=data,files={field:("audio.wav" if command.kind=="asr" else "image.upload",material.prepared,
                "audio/wav" if command.kind=="asr" else "application/octet-stream")})
        value=decode_sse_json_object(reply.body.decode("utf-8"))
        if u["mode"]=="async":
            accepted=JobIdentity.model_validate(value);self.match(accepted,command)
            return AdapterOutcome(state="pending",job_id=accepted.job_id)
        if reply.status!=200:raise ProtocolFailure("UPSTREAM_FINAL_RESPONSE_MISSING")
        if command.kind=="vision":result=RemoteDetectionResult.model_validate(value)
        else:
            if u["protocol"]=="remote-v2":
                response=RemoteTranscriptResponse.model_validate(value)
                if response.details.request_id not in (None,command.invocation_id):raise ProtocolFailure("UPSTREAM_CORRELATION_MISMATCH")
                segments=response.details.segments;text="".join(s.text for s in segments).strip()
            else:
                response=OpenAiAudioTranscript.model_validate(value);segments=response.segments;text=response.text.strip()
            if len(text)>131072:raise ProtocolFailure("UPSTREAM_TEXT_TOO_LARGE")
            result=TranscriptionResult(text=text,segments=[TranscriptionSegment(start=s.start,end=s.end,text=s.text) for s in segments],
                language_requested=manifest.language,audio_duration_seconds=manifest.audio_duration_seconds,
                engine_metadata=AsrEngineMetadata(adapter_key=u["protocol"]))
        self.validate_result(result,command,manifest)
        return AdapterOutcome(state="succeeded",result=result)

    @staticmethod
    def validate_result(result,command,manifest):
        if command.kind=="asr":
            if (not isinstance(result,TranscriptionResult) or result.language_requested!=manifest.language
                    or abs(result.audio_duration_seconds-manifest.audio_duration_seconds)>1/16000
                    or any(s.end>manifest.audio_duration_seconds+1/16000 for s in result.segments)
                    or any(b.start<a.start for a,b in zip(result.segments,result.segments[1:]))):raise ProtocolFailure("UPSTREAM_RESULT_INVALID")
        elif command.kind=="vision":
            if (not isinstance(result,RemoteDetectionResult) or result.model_id!=command.model_id
                    or (result.model_version is not None and result.model_version!=manifest.model_version)
                    or (result.image_width,result.image_height)!=(manifest.image_width,manifest.image_height)
                    or len(result.detections)>manifest.max_detections
                    or any(d.confidence<=manifest.confidence_threshold for d in result.detections)):raise ProtocolFailure("UPSTREAM_RESULT_INVALID")

    def query(self,command,config,job_id,*,result=False):
        q=config["upstream"]["query"];path=q["resultPath"] if result else q["statusPath"]
        identifier=job_id if q["idSource"]=="job" else command.invocation_id
        if not path or not identifier:raise ProtocolFailure("UPSTREAM_QUERY_UNSUPPORTED")
        reply=self._request(config,"GET",path.replace("{requestId}",identifier),timeout=q["timeoutSeconds"],
            maximum=2*1024*1024 if result else 65536)
        status=JobStatus.model_validate(decode_sse_json_object(reply.body.decode("utf-8")))
        self.match(status,command,job_id)
        return status

    def cancel(self,command,config,job_id):
        q=config["upstream"]["query"];identifier=job_id if q["idSource"]=="job" else command.invocation_id
        if not q["cancelPath"] or not identifier:raise ProtocolFailure("UPSTREAM_CANCEL_UNSUPPORTED")
        reply=self._request(config,"POST",q["cancelPath"].replace("{requestId}",identifier),timeout=q["timeoutSeconds"],maximum=16384,
            json={"invocationId":command.invocation_id,"attempt":command.attempt,"serviceRevision":command.service_revision,"configRevision":command.config_revision})
        ack=JobCancellation.model_validate(decode_sse_json_object(reply.body.decode("utf-8")));self.match(ack,command,job_id)

    def stream(self,command,material,config):
        u=config["upstream"];request=material.online
        adapter=HttpChatAdapter(config["internalBaseUrl"]+u["invokePath"],u["upstreamModel"],self._token(config),
            min(u["connectTimeoutSeconds"],300),min(u["timeoutSeconds"],300),transport=self.transport,
            verify=ssl.create_default_context(cafile=u.get("caFile")),correlation_headers={"X-Request-ID":command.invocation_id,
                "X-Inference-Attempt":str(command.attempt),"X-Inference-Service-Revision":command.service_revision,
                "X-Inference-Config-Revision":command.config_revision} if u["query"]["protocol"]!="none" else None)
        remaining=min(u["timeoutSeconds"],(as_utc(command.deadline_at)-datetime.now(timezone.utc)).total_seconds())
        stream=adapter.open([m.model_dump() for m in request.messages],request.parameters.model_dump(),monotonic()+remaining)
        try:yield from stream.events()
        finally:stream.close()
