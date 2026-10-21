"""入口 Node 无重试、无凭据透传、响应有界和配置校验。"""

import json
import httpx2 as httpx
import pytest
from pydantic import ValidationError
from backend.common.exceptions.app_error import AppError
from backend.config.host_node.node_settings import NodeSettings
from backend.config.settings.app_settings import AppSettings
from backend.service.host_node.ingress_service import NodeIngress

APP_KEY='ipk_'+'a'*32+'.'+'b'*43


def settings():
    return NodeSettings(node_id='entry',admin_token='a'*40,central_url='http://center.test',ingress_token='c'*40,metrics_enabled=False)


@pytest.mark.parametrize('reply',[httpx.Response(307,headers={'location':'http://evil.test'}),httpx.Response(500,text='PRIVATE_ERROR_BODY'),httpx.Response(200,stream=httpx.ByteStream(b'not-gzip'),headers={'content-encoding':'gzip'})])
def test_proxy_does_not_retry_redirect_or_expose_raw_errors(reply):
    calls=[]
    def server(request):calls.append(request);return reply
    proxy=NodeIngress(settings(),transport=httpx.MockTransport(server))
    with pytest.raises(AppError) as e:proxy.forward('POST','tasks/asr',[],{'Authorization':'Bearer '+APP_KEY,'Content-Type':'multipart/form-data; boundary=x'},b'x')
    assert e.value.code=='CENTER_UNAVAILABLE' and 'PRIVATE_ERROR_BODY' not in str(e.value)
    assert len(calls)==1


def test_proxy_discards_incoming_internal_tokens_and_preserves_idempotency():
    def server(request):
        assert request.headers['x-node-ingress-token']=='c'*40
        assert 'x-management-token' not in request.headers
        assert request.headers['idempotency-key']=='same'
        assert 'cookie' not in request.headers
        return httpx.Response(202,json={'data':{'id':'a'*32},'requestId':'test'})
    proxy=NodeIngress(settings(),transport=httpx.MockTransport(server))
    assert proxy.forward('POST','tasks/asr',[],{'Authorization':'Bearer '+APP_KEY,'Idempotency-Key':'same','Cookie':'private','X-Management-Token':'private','X-Node-Ingress-Token':'untrusted'},b'')[0]==202


@pytest.mark.parametrize('path',['../admin','tasks/../../node/internal/v1/executions','tasks/'+'a'*32+'/../result','http://evil.test','node/internal/v1/executions'])
def test_proxy_path_allowlist_prevents_internal_route_access(path):
    proxy=NodeIngress(settings())
    with pytest.raises(AppError) as e:proxy.forward('POST',path,[],{'Authorization':'Bearer '+APP_KEY},b'')
    assert e.value.status_code==404


def test_ingress_tokens_are_independent_and_public_requires_common_mode():
    assert settings().central_url=='http://center.test'
    with pytest.raises(ValidationError):NodeSettings(node_id='n',admin_token='a'*40,central_url='http://center.test',ingress_token='a'*40)
    with pytest.raises(ValidationError):NodeSettings(node_id='n',admin_token='a'*40,central_url='http://center.test/path',ingress_token='c'*40)
    with pytest.raises(ValidationError):AppSettings(public_api_enabled=True,ingress_token='c'*40)
    assert 'c'*40 not in json.dumps(settings().model_dump(mode='json'))
