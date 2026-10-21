"""应用隔离、原子受理和经 Node 的三类协议；全为隔离资料。"""

from io import BytesIO
import json
from types import SimpleNamespace
import httpx2 as httpx
import pytest
from sqlalchemy import select,text
from backend.schema.gateway.gateway_schema import KeyCommand
from backend.schema.invocations.invocation_schema import ApplicationBudget
from backend.service.gateway.key_service import GatewayKeys
from backend.service.gateway.request_service import GatewayRequests
from backend.service.host_node.ingress_service import NodeIngress
from backend.config.host_node.node_settings import NodeSettings
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.modules.host_node.app import close_node_app
from tests.integration.test_common_execution import case
from tests.integration.node_protocol_support import protocol_case,audio_bytes,FlaskStream

TOKEN="ingress-secret-for-tests-"+"i"*32


@pytest.fixture
def gateway(case,tmp_path):
    nodes=[]
    def create(kind="asr",protocol="remote-v2",mode="sync",query=False):
        value=protocol_case(case,tmp_path/"node",kind=kind,protocol=protocol,mode=mode,query=query);nodes.append(value.node)
        settings=case.settings.model_copy(update={"public_api_enabled":True,"ingress_token":__import__('pydantic').SecretStr(TOKEN)})
        case.app.config["APP_SETTINGS"]=settings
        keys=case.app.extensions["gateway_keys"]
        service=GatewayRequests(case.core,value.bridge);case.app.extensions["gateway_requests"]=service
        case.app.extensions["node_invocation_bridge"]=value.bridge
        issued={}
        for app in ("app-a","app-b"):
            case.core.configure_application(ApplicationBudget(application_id=app,allowed_services=[value.service],concurrency_limit=2))
            issued[app]=keys.issue(app,KeyCommand())
        def call(method,path,app="app-a",**kwargs):
            headers={"Authorization":"Bearer "+issued[app]["apiKey"],"X-Node-Ingress-Token":TOKEN}
            headers.update(kwargs.pop("headers",{}))
            return case.app.test_client().open("/api/v1"+path,method=method,headers=headers,**kwargs)
        value.call=call;value.keys=keys;value.issued=issued;value.public=service
        def upload(key="one",app="app-a",**kwargs):
            if kind=="asr":content=audio_bytes()
            else:
                from PIL import Image
                out=BytesIO();Image.new("RGB",(64,64)).save(out,"PNG");content=out.getvalue()
            data={"serviceId":value.service,"file":(BytesIO(content),"input.upload")};data.update(kwargs)
            return call("POST","/tasks/"+kind,app,headers={"Idempotency-Key":key},data=data)
        value.upload=upload
        return value
    yield create
    for node in nodes:close_node_app(node.app)


def data(response,status=200):
    assert response.status_code==status,response.get_json()
    return response.get_json()["data"]


def test_public_auth_and_keys_are_not_saved_in_plaintext(gateway):
    v=gateway()
    assert v.case.app.test_client().get('/api/v1/services').status_code==401
    assert v.call("GET","/services",headers={"Authorization":"Bearer invalid"}).status_code==401
    assert len(data(v.call("GET","/services"))["items"])==1
    with v.case.db.engine.connect() as connection:
        raw=str(connection.execute(text('select * from gateway_api_keys')).all())
    assert v.issued['app-a']['apiKey'] not in raw
    assert 'token_hash' not in json.dumps(v.keys.applications(),default=str)


@pytest.mark.parametrize("kind,protocol",[("asr","remote-v2"),("asr","openai-audio"),("vision","vision-json-v1")])
def test_public_submission_is_atomic_idempotent_and_pins_node(gateway,kind,protocol):
    v=gateway(kind,protocol);accepted=data(v.upload(),202);identifier=accepted['id']
    assert data(v.upload(),202)['id']==identifier
    lease=v.case.core.claim_round('public-test',require_node=True)[0]
    assert v.bridge.dispatch(lease)
    assert data(v.call('GET',f'/tasks/{identifier}'))['status']=='succeeded'
    assert data(v.call('GET',f'/tasks/{identifier}/result'))['result']
    assert data(v.call('GET',f'/tasks/{identifier}/monitoring'))['assignedDevice']['kind']=='unknown'
    assert data(v.call('GET',f'/tasks/{identifier}/events'))['items']
    for suffix in ('','/result','/events','/monitoring'):
        assert v.call('GET',f'/tasks/{identifier}'+suffix,'app-b').status_code==404
    assert v.call('POST',f'/tasks/{identifier}/cancel','app-b',json={}).status_code==404
    assert data(v.call('GET','/tasks','app-b'))['total']==0
    assert sum(method=='POST' for method,_ in v.calls)==1


def test_key_rotation_preserves_task_ownership_and_revocation(gateway):
    v=gateway();identifier=data(v.upload(),202)['id'];old=v.issued['app-a']
    rotated=v.keys.issue('app-a',KeyCommand(label='rotated'),replace=old['keyId'])
    assert v.call('GET',f'/tasks/{identifier}').status_code==401
    v.issued['app-a']=rotated
    assert data(v.call('GET',f'/tasks/{identifier}'))['applicationId']=='app-a'
    v.keys.revoke('app-a',rotated['keyId'])
    assert v.call('GET','/tasks').status_code==401


def test_readonly_monitoring_and_cancel_before_dispatch(gateway):
    v=gateway();identifier=data(v.upload(),202)['id']
    for _ in range(4):data(v.call('GET',f'/tasks/{identifier}/monitoring'))
    assert not v.calls
    assert data(v.call('POST',f'/tasks/{identifier}/cancel',json={}))['status']=='cancelled'
    assert not v.case.core.claim_round('worker')
    assert v.upload(serviceId='forbidden').status_code==403
    assert v.call('GET',f'/tasks/{identifier}/events?limit=201').status_code==422


def test_public_llm_finishes_and_duplicate_post_does_not_replay(gateway):
    v=gateway('llm','chat-completions-sse','stream')
    payload={'serviceId':v.service,'messages':[{'role':'user','content':'PRIVATE_PUBLIC_TEXT'}]}
    response=v.call('POST','/llm/requests',headers={'Idempotency-Key':'chat-one'},json=payload)
    assert response.status_code==200
    text=response.get_data(as_text=True);assert 'event: done' in text
    identifier=response.headers['X-Online-Request-ID']
    assert data(v.call('GET',f'/llm/requests/{identifier}'))['status']=='succeeded'
    assert v.call('GET',f'/llm/requests/{identifier}','app-b').status_code==404
    assert data(v.call('POST','/llm/requests',headers={'Idempotency-Key':'chat-one'},json=payload))['streamReplayed'] is False
    assert sum(method=='POST' for method,_ in v.calls)==1


def test_gateway_node_forwards_only_public_routes_and_center_authority(gateway):
    v=gateway();settings=v.node.settings.model_copy(update={'central_url':'http://center.test','ingress_token':__import__('pydantic').SecretStr(TOKEN)})
    def route(request):
        result=v.case.app.test_client().open(request.url.raw_path.decode(),method=request.method,data=request.content,
            headers={k:val for k,val in request.headers.items() if k.lower()!='host'},buffered=False)
        return httpx.Response(result.status_code,headers={'content-type':result.content_type},stream=FlaskStream(result))
    ingress=NodeIngress(settings,transport=httpx.MockTransport(route))
    status,kind,payload,_=ingress.forward('GET','services',[],{'Authorization':'Bearer '+v.issued['app-a']['apiKey']},b'')
    assert status==200 and json.loads(payload)['data']['items']
    from backend.common.exceptions.app_error import AppError
    with pytest.raises(AppError):ingress.forward('POST','../node/internal/v1/executions',[],{},b'')
    client=v.case.app.test_client()
    assert client.get('/api/tasks',environ_overrides={'REMOTE_ADDR':'10.0.0.7'}).status_code==403
    assert client.get('/api/admin/gateway/applications',environ_overrides={'REMOTE_ADDR':'10.0.0.7'}).status_code==403
