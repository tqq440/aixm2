"""实际公共配置启动、拒绝隐式迁移与旧资料升级。"""

from alembic import command
from pydantic import SecretStr
import pytest
from sqlalchemy import text
from backend.common.exceptions.app_error import StartupError
from backend.modules.api.app import create_app,close_app
from tests.integration.test_common_execution import case

TOKEN='startup-ingress-'+'s'*40


def enabled(case):
    return type(case.settings).model_validate({**case.settings.model_dump(),
        'public_api_enabled':True,'ingress_token':SecretStr(TOKEN)})


def test_actual_factory_accepts_public_configuration_only_after_migration(case):
    app=create_app(enabled(case))
    try:
        client=app.test_client()
        status=client.get('/api/admin/gateway/status').get_json()['data']
        assert status['enabled'] and status['managementAvailable']
        assert client.get('/api/v1/services').status_code==401
        assert client.get('/api/v1/services',headers={'X-Node-Ingress-Token':TOKEN}).status_code==401
    finally:close_app(app)


def test_public_startup_refuses_0019_without_implicit_upgrade(case):
    command.downgrade(case.migration,'20261021_0019')
    assert case.app.test_client().get('/api/admin/gateway/status').get_json()['data']['managementAvailable'] is False
    with pytest.raises(StartupError,match='0020'):create_app(enabled(case))
    with case.db.engine.connect() as c:
        assert c.scalar(text('select version_num from alembic_version'))=='20261021_0019'
        assert c.scalar(text("select count(*) from sqlite_master where type='table' and name='gateway_api_keys'"))==0


def test_0020_upgrade_preserves_existing_task_events_and_occupancy(case):
    command.downgrade(case.migration,'20261021_0019')
    case.group();case.service();case.budget();task,invocation=case.task()
    lease=case.core.claim_round('old-worker')[0];case.core.mark_sent(lease)
    before=case.core.get(invocation);events=case.events(task);capacity=case.core.capacity()
    command.upgrade(case.migration,'head');command.upgrade(case.migration,'head')
    assert case.core.get(invocation)==before and case.events(task)==events and case.core.capacity()==capacity
    with case.db.engine.connect() as c:assert c.scalar(text('select count(*) from gateway_api_keys'))==0
