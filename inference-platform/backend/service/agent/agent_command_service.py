"""固定实例启停意图；与任务准入使用同一 SQLite 写事务。"""

import hmac
from time import monotonic, sleep

from backend.common.exceptions.app_error import AppError
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.database import database_now
from backend.data.nodes.local_probe import probe_local_node
from backend.data.nodes.node_repository import NodeRepository
from backend.schema.agent.agent_schema import ManagementCommand
from backend.service.agent.agent_status_service import AgentStatusService, fresh, session_confirmed


class AgentCommandService(AgentStatusService):
    def authorize_http(self, remote_addr, token):
        if remote_addr not in {'127.0.0.1', '::1'}:
            raise AppError('AGENT_LOCAL_ONLY', '管理操作只允许本机访问。', 403)
        expected = self.settings.agent_token
        if expected is None:
            raise AppError('AGENT_TOKEN_NOT_CONFIGURED', '尚未配置本机管理令牌，请使用本机维护命令。', 503)
        if not token or not hmac.compare_digest(token.encode('utf-8'), expected.get_secret_value().encode('utf-8')):
            raise AppError('AGENT_UNAUTHORIZED', '管理令牌不正确。', 401)

    def local_instance_id(self):
        try:
            snapshot = probe_local_node()
        except OSError as exc:
            raise AppError('NODE_PROBE_FAILED', '无法核对本机身份，请检查本机读取权限。', 503) from exc
        with self.database.transaction() as session:
            node = NodeRepository.get_node_by_identity(session, snapshot['local_identity'])
            instance = NodeRepository.get_local_instance(session, node.node_id, 'asr-default') if node else None
            if instance is None:
                raise AppError('LOCAL_INSTANCE_NOT_REGISTERED', '本机尚未登记 ASR 实例，请先启动平台 Agent。', 409)
            return instance.instance_id

    def command(self, instance_id, action, command):
        if action not in {'start', 'stop'}:
            raise AppError('INVALID_AGENT_OPERATION', '仅允许启动或停止固定 ASR 实例。', 400)
        command = ManagementCommand.model_validate(command)
        if instance_id != self.local_instance_id():
            raise AppError('AGENT_LOCAL_ONLY', '不允许控制其他机器的实例。', 403)
        with self.database.transaction(immediate=True) as session:
            self._instance(session, instance_id)
            row = AgentRepository.get_control(session, instance_id)
            now = database_now(session)
            if row is None or not fresh(row, now, self.settings):
                raise AppError('ASR_AGENT_UNAVAILABLE', 'Agent 未在线或心跳已过期，不能据此确认启停。', 503)
            if row.agent_session_id != command.agent_session_id or row.command_revision != command.expected_revision:
                raise AppError('AGENT_COMMAND_STALE', '管理会话或命令版本已变化，请刷新状态后重试。', 409)
            if row.state == 'stop_unconfirmed':
                raise AppError('ASR_STOP_UNCONFIRMED', '尚未确认上一模型进程退出，不能开始新的启动会话。', 409)
            target = 'running' if action == 'start' else 'stopped'
            if row.desired_state != target:
                if action == 'start' and not session_confirmed(session, row):
                    raise AppError('ASR_STOP_IN_PROGRESS', '当前启动会话尚未结束，请等待退出确认。', 409)
                AgentRepository.update_control(row, desired_state=target, command_revision=row.command_revision + 1,
                    state='starting' if action == 'start' else ('stopped' if session_confirmed(session, row) else 'stopping'),
                    updated_at=now, last_error=None if action == 'start' else row.last_error)
        return self.status(instance_id)

    def local_command(self, action, *, wait_seconds=0):
        instance_id = self.local_instance_id()
        status = self.status(instance_id)
        if action == 'status':
            return status
        if not status.agent_session_id:
            raise AppError('ASR_AGENT_UNAVAILABLE', '请先启动平台 Agent。', 503)
        status = self.command(instance_id, action, ManagementCommand(
            agent_session_id=status.agent_session_id, expected_revision=status.command_revision))
        deadline = monotonic() + wait_seconds
        while wait_seconds and status.state not in ({'ready', 'busy'} if action == 'start' else {'stopped'}):
            if status.state in {'failed', 'stop_unconfirmed'} or not status.agent_available:
                raise AppError('ASR_MANAGEMENT_FAILED', 'ASR 未达到目标状态，请使用 status 查看最近错误。', 409)
            if monotonic() >= deadline:
                raise AppError('ASR_MANAGEMENT_WAIT_TIMEOUT', '等待超时；启停意图仍有效，请查询状态，不会强制中断当前音频。', 409)
            sleep(self.settings.poll_interval_seconds)
            status = self.status(instance_id)
        return status
