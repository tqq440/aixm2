"""4C2 隔离协议服务；音频、图片、文本均为测试生成，不接真实模型。"""

from io import BytesIO
import json
from types import SimpleNamespace
import wave

import httpx2 as httpx
from sqlalchemy import select

from backend.data.host_node.execution_models import HostNodeExecution
from backend.data.tasks.task_models import TaskRecord
from backend.data.files.upload_store import LocalUploadStore
from backend.modules.host_node.app import create_node_app
from backend.service.host_node.http_adapter import NodeHttpAdapter
from backend.service.host_node.input_service import CentralMaterialSource
from backend.service.host_node.execution_client import NodeExecutionClient
from backend.service.host_node.migration_service import upgrade_node_database
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.schema.catalog.service_schema import SaveService,CheckTarget,ActivateService
from tests.integration.host_node_support import node_settings,config,register,bind,send,data,Clock,LLM_REPORT,VISION_REPORT,REPORT
from tests.integration.test_node_execution_bridge import EXECUTOR


def audio_bytes():
    output=BytesIO()
    with wave.open(output,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b"\x00\x00"*1600)
    return output.getvalue()


class FlaskStream(httpx.SyncByteStream):
    def __init__(self,response):self.response=response
    def __iter__(self):yield from self.response.response
    def close(self):self.response.close()


def protocol_case(case,folder,*,kind="asr",protocol="remote-v2",mode="sync",query=False,base_url=None,extra_reports=None,inline_result=False):
    options={}
    if extra_reports:
        options["report_tokens"]={"NODE_REPORT_TOKEN_ASR":REPORT,"NODE_REPORT_TOKEN_LLM":LLM_REPORT,"NODE_REPORT_TOKEN_VISION":VISION_REPORT,**extra_reports}
    settings=node_settings(folder,business_enabled=True,executor_token=EXECUTOR,metrics_enabled=False,**options)
    upgrade_node_database(settings)
    clock=Clock();calls=[];phase={"state":"running","progress":35.0,"statusCode":200,"resultCode":200,"result":False}
    node=SimpleNamespace(settings=settings,clock=clock)
    model="detector-fixture" if kind=="vision" else kind+"-fixture"
    version="remote-v1" if kind=="vision" else "fixture-v1"
    result={"text":"SYNTHETIC_PRIVATE_RESULT","segments":[],"languageRequested":None,"audioDurationSeconds":0.1}
    if kind=="vision":result={"schemaVersion":"vision-json-v1","modelId":model,"modelVersion":version,
        "imageWidth":64,"imageHeight":64,"detections":[{"classId":100,"className":"custom","confidence":0.9,
            "box":{"xMin":1,"yMin":2,"xMax":10,"yMax":20}}]}
    def context():
        with node.service.database.transaction() as session:
            command=session.scalar(select(HostNodeExecution).order_by(HostNodeExecution.created_at.desc())).command_json
        return {k:command[k] for k in ("invocationId","attempt","serviceRevision","configRevision")}|{"jobId":"job-a"}
    def upstream(request):
        calls.append((request.method,request.url.path))
        if request.method=="POST" and request.url.path=="/infer":
            if mode=="async":return httpx.Response(202,json=context())
            if kind=="llm":
                frames=[]
                for text,finish in (("SYNTHETIC_PRIVATE_DELTA",None),("","stop")):
                    chunk={"id":"stream-one","object":"chat.completion.chunk","model":"fixture-upstream",
                        "choices":[{"index":0,"delta":{"content":text},"finish_reason":finish}]}
                    frames.append("data: "+json.dumps(chunk)+"\n\n")
                if not phase.get("disconnect"):frames.append("data: [DONE]\n\n")
                return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=httpx.ByteStream("".join(frames).encode()))
            if kind=="vision":return httpx.Response(200,json=result)
            if protocol=="remote-v2":return httpx.Response(200,json={"success":True,"engine":"sensevoice",
                "details":{"request_id":context()["invocationId"],"segments":[{"start":0,"end":0.1,"text":"SYNTHETIC_PRIVATE_RESULT"}]}})
            return httpx.Response(200,json={"text":"SYNTHETIC_PRIVATE_RESULT","segments":[]})
        if request.method=="POST" and "/cancel/" in request.url.path:return httpx.Response(200,json=context()|{"accepted":True})
        code=phase["resultCode"] if "/result/" in request.url.path else phase["statusCode"]
        if code!=200:return httpx.Response(code,headers={"Retry-After":str(phase.get("retryAfter",30))},text="UNFILTERED_PRIVATE_ERROR")
        value=context()|{"state":phase["state"],"progress":phase["progress"],"stage":"infer",
            "actualDevice":{"kind":"gpu","deviceId":"0"}}
        if phase.get("wrongIdentity"):value["attempt"]+=1
        if phase.get("result") or "/result/" in request.url.path:value["result"]=result
        return httpx.Response(200,json=value)
    adapter=NodeHttpAdapter(settings,transport=httpx.MockTransport(upstream))
    node.app=create_node_app(settings,clock=clock,execution_adapter=adapter);node.client=node.app.test_client()
    node.service=node.app.extensions["host_node_service"]
    definition=config(kind);definition["modelId"]=model
    if base_url:definition["internalBaseUrl"]=base_url
    definition["upstream"]={"protocol":protocol,"invokePath":"/infer","mode":mode,"upstreamModel":"fixture-upstream",
        "query":{"protocol":"job-json-v1","statusPath":"/status/{requestId}","resultPath":"/result/{requestId}",
                 "cancelPath":"/cancel/{requestId}"} if query else {"protocol":"none"}}
    if inline_result:
        definition["upstream"]["query"].pop("resultPath",None)
        definition["upstream"]["query"]["resultInStatus"]=True
    entry=bind(node,register(node,kind=kind,definition=definition))
    data(send(node,entry,token=LLM_REPORT if kind=="llm" else VISION_REPORT if kind=="vision" else REPORT))
    service=kind+"-main";case.group();case.service(service,kind)
    target={"targetId":"primary","name":"isolated","baseUrl":definition["internalBaseUrl"],"invokePath":"/infer","healthPath":"/health",
        "upstreamModel":"fixture-upstream","resourceGroupIds":["shared"],"node":{"nodeId":settings.node_id,"serviceId":"asr",
            "configRevision":entry["configRevision"],"baseUrl":"http://127.0.0.1:8020","executorTokenEnv":"TEST_NODE_EXECUTOR"}}
    if mode=="async":
        target["monitoring"]={"protocol":"job-json-v1","statusPath":"/status/{requestId}","reportsProgress":True,"reportsDevice":True}
        if inline_result:target["monitoring"]["resultInStatus"]=True
        else:target["monitoring"]["resultPath"]="/result/{requestId}"
    cfg={**case.definitions[service],"protocol":protocol,"invocationMode":mode,"modelId":model,"modelVersion":version,"maxFileBytes":(10 if kind=="vision" else 20)*1024*1024,"targets":[target]}
    current=case.entries[service]
    current=case.catalog.save(service,SaveService.model_validate({"config":cfg,"expectedVersion":current["version"]}))
    case.catalog.check(service,"primary",CheckTarget(expected_version=current["version"],revision=current["draftRevision"]))
    current=case.catalog.activate(service,ActivateService(expected_version=current["version"],revision=current["draftRevision"],enabled=True))
    case.definitions[service]=cfg;case.entries[service]=current;case.budget()
    def transport(request):
        response=node.app.test_client().open(request.url.path,method=request.method,data=request.content,
            headers={k:v for k,v in request.headers.items() if k.lower()!="host"},buffered=False)
        return httpx.Response(response.status_code,headers={"content-type":response.content_type},stream=FlaskStream(response))
    factory=lambda binding:NodeExecutionClient(binding,environ={"TEST_NODE_EXECUTOR":EXECUTOR},transport=httpx.MockTransport(transport))
    bridge=NodeInvocationBridge(case.core,client_factory=factory,material_source=CentralMaterialSource(case.core))
    def task():
        owner,invocation=case.task(service)
        if kind=="asr":content=audio_bytes();media="audio"
        else:
            from PIL import Image
            buffer=BytesIO();Image.new("RGB",(64,64)).save(buffer,format="PNG");content=buffer.getvalue();media="image"
        stored=LocalUploadStore(case.settings.upload_dir,media_type=media).save(BytesIO(content),max_bytes=20*1024*1024)
        with case.db.transaction(immediate=True) as session:
            row=session.get(TaskRecord,owner);row.input_ref=stored.input_ref;row.input_sha256=stored.sha256;row.input_size_bytes=stored.size_bytes
            if kind=="asr":row.audio_duration_seconds=0.1;row.parameters={"language":None}
        return owner,case.core.claim_round("protocol-worker")[0]
    return SimpleNamespace(case=case,node=node,bridge=bridge,task=task,service=service,calls=calls,phase=phase,result=result,entry=entry,
        monitor=node.app.extensions["host_node_monitor"],executor=node.app.extensions["host_node_executions"])
