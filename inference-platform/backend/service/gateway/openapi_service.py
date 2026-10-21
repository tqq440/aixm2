"""Node 对外公共协议；与内部执行/管理凭据分开。"""

from backend.schema.gateway.gateway_schema import SubmitOnline,PublicPage,PublicRequestView,DownloadQuery
from backend.schema.invocations.invocation_schema import EventQuery
from backend.schema.common.api_schema import ErrorResponse
from backend.schema.host_node.execution_schema import TaskMonitoringSnapshot
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.host_node.upstream_schema import RemoteDetectionResult


def gateway_openapi():
    schemas={}
    for model in (SubmitOnline,PublicRequestView,ErrorResponse,TaskMonitoringSnapshot,TranscriptionResult,RemoteDetectionResult):
        value=model.model_json_schema(by_alias=True,ref_template="#/components/schemas/{model}")
        schemas.update(value.pop("$defs",{}));schemas[model.__name__]=value
    def reference(name):return {"$ref":"#/components/schemas/"+name}
    def envelope(value):return {"type":"object","required":["data","requestId"],"properties":{"data":value,"requestId":{"type":"string"}}}
    generic={"type":"object"};request=reference("PublicRequestView")
    page={"type":"object","required":["items","total","page","pageSize"],"properties":{"items":{"type":"array","items":request},"total":{"type":"integer"},"page":{"type":"integer"},"pageSize":{"type":"integer"}}}
    events={"type":"object","required":["items","hasMore","nextSequence","historyNotice"],"properties":{"items":{"type":"array","items":{"type":"object","properties":{"sequence":{"type":"integer"},"eventId":{"type":"string"},"invocationId":{"type":["string","null"]},"eventType":{"type":"string"},"source":{"type":"string"},"occurredAt":{"type":"string","format":"date-time"},"details":generic}}},"hasMore":{"type":"boolean"},"nextSequence":{"type":"integer"},"historyNotice":{"type":"string"}}}
    monitor={"type":"object","properties":{"invocationId":{"type":"string"},"platform":generic,"monitoring":{"anyOf":[reference("TaskMonitoringSnapshot"),{"type":"null"}]},"assignedDevice":{"type":["object","null"]},"remoteState":{"type":["string","null"]},"resultPending":{"type":["boolean","null"]},"resultSaved":{"type":["boolean","null"]},"lateResultSaved":{"type":["boolean","null"]},"nodeQueryError":{"type":["string","null"]},"executionError":{"type":["string","null"]},"notice":{"type":["string","null"]}}}
    result={"type":"object","properties":{"taskId":{"type":"string"},"lateResult":{"type":"boolean"},"originalOutcome":{"type":["string","null"]},"result":{"anyOf":[reference("TranscriptionResult"),reference("RemoteDetectionResult")]}}}
    paths={}
    def operation(path,method,summary,output=generic,status="200",query_model=None,body=None,idempotent=False):
        params=[]
        if "{id}" in path:params.append({"name":"id","in":"path","required":True,"schema":{"type":"string","pattern":"^[a-f0-9]{32}$"}})
        if query_model:
            raw=query_model.model_json_schema(by_alias=True)
            params.extend({"name":name,"in":"query","required":False,"schema":schema} for name,schema in raw['properties'].items())
        if idempotent:params.append({"name":"Idempotency-Key","in":"header","required":True,"schema":{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$"}})
        responses={status:{"description":summary,"content":{"application/json":{"schema":envelope(output)}}}}
        for code in (400,401,403,404,409,413,415,422,429,503):responses[str(code)]={"description":"错误码以 error.code 为准；404不暴露其他应用资源；503提交结果不明时保留幂等键。","content":{"application/json":{"schema":reference("ErrorResponse")}}}
        value={"summary":summary,"operationId":method+'_'+path.replace('/','_').replace('{','').replace('}',''),'security':[{"ApplicationKey":[]}],"parameters":params,"responses":responses}
        if body:value['requestBody']=body
        paths[path]={**paths.get(path,{}),method:value}
        return value
    empty={"required":True,"content":{"application/json":{"schema":{"type":"object","additionalProperties":False}}}}
    operation('/api/v1/services','get','查询当前应用允许使用的服务，不公开地址或凭据')
    for kind in ('asr','vision'):
        props={"serviceId":{"type":"string"},"file":{"type":"string","format":"binary"}}
        if kind=='asr':props['language']={"type":"string","description":"仅支持语言控制的OpenAI Audio服务可填，Remote-V2省略"}
        else:props['parameters']={"type":"string","description":"可选JSON字符串：confidenceThreshold、iouThreshold、maxDetections"}
        operation('/api/v1/tasks/'+kind,'post','持久化任务、文件和outbox后返回；重复键返回原任务',request,'202',body={"required":True,"content":{"multipart/form-data":{"schema":{"type":"object","required":["serviceId","file"],"additionalProperties":False,"properties":props}}}},idempotent=True)
    operation('/api/v1/tasks','get','只读本应用任务列表',page,query_model=PublicPage)
    operation('/api/v1/tasks/{id}','get','只读任务详情',request)
    operation('/api/v1/tasks/{id}/result','get','已保存结果；未保存返回409',result)
    download=operation('/api/v1/tasks/{id}/result/download','get','下载平台已保存的结果；TXT只适用于ASR',query_model=DownloadQuery)
    download['responses']['200']['content']={'application/json':{'schema':{'anyOf':[reference('TranscriptionResult'),reference('RemoteDetectionResult')]}},'text/plain':{'schema':{'type':'string'}}}
    operation('/api/v1/tasks/{id}/cancel','post','请求取消；ACK不确认远端停止',body=empty)
    for root in ('/api/v1/tasks/{id}','/api/v1/llm/requests/{id}'):
        operation(root+'/events','get','只读游标事件，默认50最多200',events,query_model=EventQuery)
        operation(root+'/monitoring','get','只读已保存快照，不即时查询服务',monitor)
    online=operation('/api/v1/llm/requests','post','自定义SSE：accepted/delta/done/error；重复提交返回JSON，不重放token',body={'required':True,'content':{'application/json':{'schema':reference('SubmitOnline')}}},idempotent=True)
    online['responses']['200']['content']={'text/event-stream':{'schema':{'type':'string'},'example':'event: delta\ndata: {"requestId":"...","text":"hello"}\n\n'},'application/json':{'schema':envelope({'type':'object','properties':{'request':request,'streamReplayed':{'const':False}}})}}
    operation('/api/v1/llm/requests/{id}','get','只读本应用在线元数据，不保存聊天正文',request)
    operation('/api/v1/llm/requests/{id}/cancel','post','请求取消在线调用，未知仍占用容量',body=empty)
    operation('/api/v1/openapi.json','get','本协议文档')
    return {'openapi':'3.1.0','info':{'title':'Inference Gateway Public API','version':'v1','description':'外部访问入口Node并携带应用Key。所有GET只读；POST提交使用稳定幂等键。接口不是OpenAI兼容API。'},'servers':[{'url':'/'}],'paths':paths,'components':{'schemas':schemas,'securitySchemes':{'ApplicationKey':{'type':'http','scheme':'bearer','description':'中心签发的独立应用Key；不能调用Node内部执行或管理接口。'}}}}
