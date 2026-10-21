"""身份旁路、故障、迁移、下载与只读语义。"""

from io import BytesIO
import json
import pytest
from alembic import command
from sqlalchemy import select,text
from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.schema.gateway.gateway_schema import KeyCommand
from backend.schema.invocations.invocation_schema import ApplicationBudget
from backend.data.invocations.invocation_models import InferenceInvocation
from tests.integration.test_gateway_public import gateway,data,TOKEN
from tests.integration.test_common_execution import case
from tests.integration.node_protocol_support import audio_bytes


@pytest.mark.parametrize('path',['/api/tasks','/api/tasks/'+'a'*32,'/api/tasks/'+'a'*32+'/result','/api/admin/common-execution/capacity','/api/admin/service-catalog/services','/api/llm/requests','/api/admin/gateway/applications'])
def test_remote_legacy_and_admin_routes_cannot_bypass_application_auth(gateway,path):
    v=gateway();client=v.case.app.test_client()
    assert client.get(path,headers={'Authorization':'Bearer '+v.issued['app-a']['apiKey']},environ_overrides={'REMOTE_ADDR':'10.1.2.3'}).status_code==403


def test_changed_content_conflicts_and_uploaded_duplicate_is_cleaned_up(gateway):
    v=gateway();identifier=data(v.upload(),202)['id']
    changed=bytearray(audio_bytes());changed[-1]=1
    assert v.upload(file=(BytesIO(changed),'private.wav')).status_code==409
    assert data(v.upload(),202)['id']==identifier
    assert len(list(v.case.settings.upload_dir.glob('objects/*')))==1
    with v.case.db.engine.connect() as c:
        assert c.scalar(text('select count(*) from idempotency_records'))==1
        assert c.scalar(text('select count(*) from inference_queue_outboxes'))==1


def test_service_permission_removed_hides_existing_tasks_but_keeps_ownership(gateway):
    v=gateway();identifier=data(v.upload(),202)['id']
    v.case.core.configure_application(ApplicationBudget(application_id='app-a',expected_version=1,allowed_services=['different-service']))
    assert v.call('GET',f'/tasks/{identifier}').status_code==404
    assert data(v.call('GET','/tasks'))['total']==0
    assert v.upload('new').status_code==403
    assert v.call('GET','/services').status_code==200


def test_downloads_require_same_application_and_are_readonly(gateway):
    v=gateway();identifier=data(v.upload(),202)['id']
    assert v.call('GET',f'/tasks/{identifier}/result/download').status_code==409
    v.bridge.dispatch(v.case.core.claim_round('worker')[0]);before=len(v.calls)
    for format in ('json','txt'):
        r=v.call('GET',f'/tasks/{identifier}/result/download?format={format}')
        assert r.status_code==200 and 'attachment' in r.headers['Content-Disposition']
        assert v.call('GET',f'/tasks/{identifier}/result/download?format={format}','app-b').status_code==404
    assert len(v.calls)==before


def test_llm_eof_and_consumer_close_are_unknown_and_not_replayed(gateway):
    v=gateway('llm','chat-completions-sse','stream');v.phase['disconnect']=True
    payload={'serviceId':v.service,'messages':[{'role':'user','content':'SENSITIVE_CHAT_BODY'}]}
    r=v.call('POST','/llm/requests',headers={'Idempotency-Key':'eof'},json=payload)
    text_body=r.get_data(as_text=True);identifier=r.headers['X-Online-Request-ID']
    assert 'event: error' in text_body and 'event: done' not in text_body
    assert data(v.call('GET',f'/llm/requests/{identifier}'))['permitState']=='uncertain'
    assert data(v.call('POST','/llm/requests',headers={'Idempotency-Key':'eof'},json=payload))['streamReplayed'] is False
    with v.case.db.engine.connect() as c:
        for name in ('online_requests','inference_task_events','gateway_api_keys'):
            raw=str(c.execute(text('select * from '+name)).all())
            assert 'SENSITIVE_CHAT_BODY' not in raw and v.issued['app-a']['apiKey'] not in raw


def test_consumer_closes_before_first_upstream_byte_keeps_unknown(gateway):
    v=gateway('llm','chat-completions-sse','stream')
    r=v.call('POST','/llm/requests',headers={'Idempotency-Key':'close'},json={'serviceId':v.service,'messages':[{'role':'user','content':'hello'}]},buffered=False)
    identifier=r.headers['X-Online-Request-ID'];r.close()
    assert data(v.call('GET',f'/llm/requests/{identifier}'))['permitState']=='uncertain'
    assert not v.calls


def test_gateway_admin_writes_require_local_json_and_key_values_are_only_once(gateway):
    v=gateway();client=v.case.app.test_client()
    url='/api/admin/gateway/applications/app-a/keys'
    assert client.post(url,json={},headers={'Origin':'http://evil.test'}).status_code==403
    assert client.post(url,data='{}',content_type='text/plain').status_code==415
    created=data(client.post(url,json={'label':'admin-test'}),201)
    assert 'apiKey' in created
    raw=data(client.get('/api/admin/gateway/applications'))
    assert created['apiKey'] not in json.dumps(raw,default=str)
    assert 'tokenHash' not in json.dumps(raw,default=str)


def test_gateway_migration_preserves_common_history_and_refuses_loss(gateway):
    v=gateway();identifier=data(v.upload(),202)['id']
    with pytest.raises(RuntimeError,match='拒绝删除'):command.downgrade(v.case.migration,'20261021_0019')
    with v.case.db.engine.connect() as c:assert c.scalar(text('select version_num from alembic_version'))=='20261021_0020'
    command.upgrade(v.case.migration,'head')
    assert data(v.call('GET',f'/tasks/{identifier}'))['applicationId']=='app-a'


def test_public_document_paths_and_response_schema_match(gateway):
    v=gateway();document=v.call('GET','/openapi.json').get_json()
    assert document['openapi']=='3.1.0'
    described={(path,method) for path,methods in document['paths'].items() for method in methods}
    actual=set()
    for rule in v.case.app.url_map.iter_rules():
        if rule.endpoint.startswith('gateway_public.'):
            path=rule.rule.replace('<identifier>','{id}')
            for verb in rule.methods-{'HEAD','OPTIONS'}:
                if '<kind>' in path:
                    for kind in ('asr','vision'):actual.add((path.replace('<kind>',kind),verb.lower()))
                else:actual.add((path,verb.lower()))
    assert actual==described
    assert not any('/admin/' in path or '/node/internal/' in path for path,_ in described)


def test_node_admin_snapshot_gets_do_not_query_until_explicit_refresh(gateway):
    v=gateway();v.case.app.extensions['gateway_administration'].bridge=v.bridge
    client=v.case.app.test_client()
    assert data(client.get('/api/admin/gateway/nodes'))['items'][0]['stale']
    assert not v.calls
    current=data(client.post('/api/admin/gateway/nodes/refresh',json={}))
    assert current['items'][0]['snapshot']['acceptingRequests']
    assert not v.calls  # Node availability itself only reads lifecycle state.
