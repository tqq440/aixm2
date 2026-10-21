"""外部→入口Node→中心→目标Node：真正回环HTTP，无浏览器、无真实模型。"""

from copy import deepcopy
from io import BytesIO
import json
import httpx2 as httpx
import pytest
from backend.config.host_node.node_settings import NodeSettings
from backend.modules.host_node.app import create_node_app,close_node_app
from backend.service.host_node.migration_service import upgrade_node_database
from backend.service.host_node.execution_client import NodeExecutionClient
from backend.service.host_node.input_service import CentralMaterialSource
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.gateway.request_service import GatewayRequests
from backend.service.invocations.node_runner import NodeExecutorRunner
from backend.schema.catalog.service_schema import SaveService,CheckTarget,ActivateService
from tests.integration.test_gateway_public import gateway,data,TOKEN
from tests.integration.test_common_execution import case
from tests.integration.node_protocol_support import audio_bytes
from tests.integration.test_node_execution_bridge import EXECUTOR
from tests.e2e.test_node_protocol_http import serving


@pytest.mark.parametrize('kind,protocol,mode',[('asr','remote-v2','sync'),('vision','vision-json-v1','sync'),('llm','chat-completions-sse','stream')])
def test_external_application_uses_only_entry_node_without_web_login(gateway,tmp_path,kind,protocol,mode):
    v=gateway(kind,protocol,mode)
    with serving(v.node.app) as target_url:
        cfg=deepcopy(v.case.definitions[v.service]);cfg['targets'][0]['node']['baseUrl']=target_url
        entry=v.case.catalog.save(v.service,SaveService.model_validate({'config':cfg,'expectedVersion':v.case.entries[v.service]['version']}))
        v.case.catalog.check(v.service,'primary',CheckTarget(expected_version=entry['version'],revision=entry['draftRevision']))
        v.case.catalog.activate(v.service,ActivateService(expected_version=entry['version'],revision=entry['draftRevision'],enabled=True))
        bridge=NodeInvocationBridge(v.case.core,client_factory=lambda binding:NodeExecutionClient(binding,environ={'TEST_NODE_EXECUTOR':EXECUTOR}),material_source=CentralMaterialSource(v.case.core))
        v.case.app.extensions['gateway_requests']=GatewayRequests(v.case.core,bridge)
        with serving(v.case.app) as center_url:
            node_settings=NodeSettings(node_id='entry',database_path=tmp_path/'entry/node.sqlite3',log_dir=tmp_path/'entry/logs',admin_token='e'*40,central_url=center_url,ingress_token=TOKEN,metrics_enabled=False)
            upgrade_node_database(node_settings);app=create_node_app(node_settings)
            try:
                with serving(app) as url,httpx.Client(base_url=url,trust_env=False,timeout=15) as client:
                    headers={'Authorization':'Bearer '+v.issued['app-a']['apiKey'],'Idempotency-Key':'real-http-one'}
                    assert client.get('/api/v1/services',headers=headers).status_code==200
                    if kind=='llm':
                        payload={'serviceId':v.service,'messages':[{'role':'user','content':'http test'}]}
                        reply=client.post('/api/v1/llm/requests',headers=headers,json=payload)
                        assert 'event: done' in reply.text
                        identifier=reply.headers['X-Online-Request-ID']
                        assert client.get('/api/v1/llm/requests/'+identifier,headers=headers).json()['data']['status']=='succeeded'
                    else:
                        if kind=='asr':content=audio_bytes()
                        else:
                            from PIL import Image
                            stream=BytesIO();Image.new('RGB',(64,64)).save(stream,'PNG');content=stream.getvalue()
                        arguments={'headers':headers,'data':{'serviceId':v.service},'files':{'file':('input.bin',content)}}
                        first=client.post('/api/v1/tasks/'+kind,**arguments);assert first.status_code==202,first.text
                        identifier=first.json()['data']['id']
                        assert client.post('/api/v1/tasks/'+kind,**arguments).json()['data']['id']==identifier
                        runner=NodeExecutorRunner(v.case.app.extensions['queue_service'],bridge,workers=1)
                        try:
                            runner.tick()
                            for future in runner.futures:future.result(timeout=10)
                        finally:runner.close()
                        assert client.get('/api/v1/tasks/'+identifier,headers=headers).json()['data']['status']=='succeeded'
                        assert client.get('/api/v1/tasks/'+identifier+'/result/download',headers=headers).status_code==200
                    assert sum(method=='POST' for method,_ in v.calls)==1
                    assert client.get('/api/v1/services').status_code==401
            finally:close_node_app(app)
