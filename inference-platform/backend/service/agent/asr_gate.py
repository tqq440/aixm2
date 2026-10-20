"""受管 ASR 的提交、通知领取及模型启动准入；与停止意图共用写事务。"""

from contextlib import contextmanager

from backend.common.exceptions.app_error import AppError
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.database import database_now
from backend.data.nodes.node_repository import NodeRepository
from backend.utils.time.time_utils import as_utc


class AsrGate:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings

    @staticmethod
    def _managed(session):
        row = AgentRepository.get_local_control(session)
        if row is not None:
            return row, True
        # 只有从未登记的旧隔离库保留独立 Worker 路径；登记后必须由 Agent 管理。
        _, total = NodeRepository.list_instances(session, page=1, page_size=1)
        return None, total > 0

    def _fresh(self, session, row):
        age = (as_utc(database_now(session)) - as_utc(row.heartbeat_at)).total_seconds()
        return 0 <= age <= self.settings.runtime_stale_seconds

    def validate_submission(self, session, model_id, model_version):
        row, managed = self._managed(session)
        if not managed:
            return
        if row is None or not self._fresh(session, row):
            raise AppError("ASR_AGENT_UNAVAILABLE", "ASR 节点 Agent 尚未运行或心跳已过期，请检查服务管理状态。", 503)
        if row.desired_state != "running" or row.state in {"stopping", "stop_unconfirmed"}:
            raise AppError("ASR_SERVICE_STOPPED", "ASR 正在停止或尚未启动，暂不接收新音频；历史任务仍可查询。", 503)
        if row.state not in {"ready", "busy"} or not row.model_ready:
            raise AppError("ASR_SERVICE_NOT_READY", "ASR 模型尚未就绪，请等待模型加载完成。", 503)
        instance = NodeRepository.get_instance(session, row.instance_id)
        if instance is None or (instance.model_id, instance.model_version) != (model_id, model_version):
            raise AppError("ASR_INSTANCE_MODEL_MISMATCH", "请求模型与受管 ASR 实例的固定版本不匹配。", 409)

    def can_claim(self, session, worker_session_id):
        row, managed = self._managed(session)
        if not managed:
            return True
        return bool(row is not None and row.worker_session_id == worker_session_id
                    and row.desired_state == "running" and row.state in {"ready", "busy"}
                    and row.model_ready and self._fresh(session, row))

    def stop_requested(self, *, agent_session_id, worker_session_id):
        with self.database.session_factory() as session:
            row, managed = self._managed(session)
            if not managed:
                return False
            return bool(row is None or row.agent_session_id != agent_session_id
                        or row.worker_session_id != worker_session_id
                        or row.desired_state != "running"
                        or row.state in {"stopping", "stop_unconfirmed", "stopped", "failed"}
                        or not self._fresh(session, row))

    @contextmanager
    def spawn_guard(self, *, agent_session_id, worker_session_id):
        """spawn/send 在同一写锁内完成；报告在外面写入，避免嵌套事务死锁。"""
        with self.database.transaction(immediate=True) as session:
            row, managed = self._managed(session)
            if not managed:
                yield True
                return
            if (row is None or agent_session_id is None
                    or row.agent_session_id != agent_session_id
                    or row.worker_session_id != worker_session_id):
                raise AppError("WORKER_AGENT_REQUIRED", "已登记的 ASR 实例只能由当前节点 Agent 启动。", 409)
            yield bool(row.desired_state == "running"
                       and row.state in {"starting", "ready", "busy"}
                       and self._fresh(session, row))
