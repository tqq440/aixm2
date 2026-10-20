"""本机管理身份和边界验证；控制行是隔离夹具，不代表真实模型。"""

from datetime import timedelta

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
import pytest

from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.database.database import database_now
from backend.schema.nodes.node_schema import LocalNodeSnapshot


@pytest.fixture
def managed_api(app_factory, monkeypatch):
    app = app_factory(agent_token=SecretStr('s' * 40))
    config = Config(str(PROJECT_ROOT / 'alembic.ini'))
    config.attributes['settings'] = app.config['APP_SETTINGS']
    command.upgrade(config, 'head')
    snapshot = LocalNodeSnapshot(local_identity='a' * 64, hostname='test', os_name='Windows',
                                os_version='test', architecture='AMD64', cpu_count=4)
    registry = app.extensions['node_registry_service']
    registry.probe = lambda: snapshot
    monkeypatch.setattr('backend.service.agent.agent_command_service.probe_local_node', lambda: snapshot.model_dump())
    receipt = registry.register_local()
    db = app.extensions['database']
    instance = receipt.instance.instance_id
    with db.transaction(immediate=True) as session:
        now = database_now(session)
        session.add(AgentControl(instance_id=instance, agent_session_id='b' * 32, agent_pid=1234,
                    agent_started_at=now, heartbeat_at=now, updated_at=now, state='stopped', desired_state='stopped'))
    return app, db, instance, '/api/instances/' + instance + '/management'


def post(app, path, body=None, token='s' * 40, **kwargs):
    headers = {'X-Management-Token': token} if token is not None else {}
    return app.test_client().post(path, headers=headers,
        json=body if body is not None else {'agentSessionId':'b' * 32, 'expectedRevision':0}, **kwargs)


def test_management_read_and_default_stop_does_not_load_model(managed_api):
    app, _, instance, url = managed_api
    view = app.test_client().get(url).get_json()['data']
    assert view['state'] == 'stopped' and not view['modelReady'] and not view['acceptingTasks']
    assert app.test_client().get(url + '/sessions').get_json()['data']['total'] == 0
    assert app.test_client().get('/api/instances/' + instance).get_json()['data']['instance']['managementMode'] == 'node_agent'
    assert app.test_client().get('/api/nodes').get_json()['data']['items'][0]['agentStatus'] == 'online'
    with pytest.raises(Exception) as error:
        app.extensions['node_registry_service'].observe_local()
    assert error.value.code == 'INSTANCE_MANAGED_BY_AGENT'


@pytest.mark.parametrize('token', [None, '', 'wrong'])
def test_mutation_needs_token_and_never_echoes_it(managed_api, token):
    app, _, _, url = managed_api
    response = post(app, url + '/start', token=token)
    assert response.status_code == 401
    assert response.get_json()['error']['code'] == 'AGENT_UNAUTHORIZED'
    assert app.test_client().get(url).get_json()['data']['commandRevision'] == 0
    assert 's' * 40 not in response.get_data(as_text=True)


def test_no_token_configuration_disables_http_mutation(managed_api):
    app, _, _, url = managed_api
    service = app.extensions['agent_service']
    service.settings = service.settings.model_copy(update={'agent_token':None})
    assert post(app, url + '/start').status_code == 503


def test_remote_management_rejected(managed_api):
    app, _, _, url = managed_api
    assert post(app, url + '/start', environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).status_code == 403


@pytest.mark.parametrize('body', [
    {'agentSessionId':'b' * 32, 'expectedRevision':0, 'command':'cmd.exe'},
    {'agentSessionId':'b' * 32, 'expectedRevision':True},
    {'agentSessionId':'b' * 32, 'expectedRevision':-1},
    {'agentSessionId':'b' * 32, 'expectedRevision':'0'},
    {'agentSessionId':'invalid', 'expectedRevision':0}, [],
])
def test_management_validates_body(managed_api, body):
    app, _, _, url = managed_api
    assert post(app, url + '/start', body).status_code == 422


def test_stale_and_duplicate_commands_have_no_extra_launch(managed_api):
    app, _, _, url = managed_api
    first = post(app, url + '/start')
    assert first.status_code == 202
    assert first.get_json()['data']['state'] == 'starting'
    assert post(app, url + '/start').status_code == 409
    duplicate = post(app, url + '/start', {'agentSessionId':'b'*32, 'expectedRevision':1})
    assert duplicate.status_code == 202 and duplicate.get_json()['data']['commandRevision'] == 1
    assert app.test_client().get(url + '/sessions').get_json()['data']['total'] == 0
    assert post(app, url + '/stop', {'agentSessionId':'c'*32, 'expectedRevision':1}).status_code == 409
    assert post(app, url + '/stop', {'agentSessionId':'b'*32, 'expectedRevision':1}).status_code == 202
    assert post(app, url + '/stop', {'agentSessionId':'b'*32, 'expectedRevision':2}).get_json()['data']['commandRevision'] == 2


def test_expired_agent_never_authorizes_or_claims_exit(managed_api):
    app, db, instance, url = managed_api
    with db.transaction(immediate=True) as session:
        row = session.get(AgentControl, instance)
        row.heartbeat_at -= timedelta(seconds=60)
        row.state = 'ready'
        row.desired_state = 'running'
        row.worker_session_id = 'd' * 32
        row.model_ready = True
        session.add(ManagedInstanceSession(worker_session_id='d'*32, instance_id=instance, agent_session_id='b'*32,
                    agent_pid=1234,state='ready',started_at=row.agent_started_at))
    assert post(app, url + '/stop').status_code == 503
    view = app.test_client().get(url).get_json()['data']
    assert view['state'] == 'unknown' and not view['agentAvailable'] and not view['modelReady']
    assert view['exitConfirmedAt'] is None


@pytest.mark.parametrize('query', ['?pageSize=1001', '?page=0', '?extra=1', '?page=1&page=2'])
def test_history_is_bounded_and_strict(managed_api, query):
    app, _, _, url = managed_api
    assert app.test_client().get(url + '/sessions' + query).status_code == 422


def test_probe_failure_is_actionable(managed_api, monkeypatch):
    app, _, _, url = managed_api
    def fail():
        raise OSError('private source')
    monkeypatch.setattr('backend.service.agent.agent_command_service.probe_local_node', fail)
    response = post(app, url + '/start')
    assert response.status_code == 503 and response.get_json()['error']['code'] == 'NODE_PROBE_FAILED'
    assert 'private source' not in response.get_data(as_text=True)
