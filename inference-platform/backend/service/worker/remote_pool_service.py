"""单一远端调用池管理多个固定槽；每槽拥有独立进程和既有执行租约。"""

import os
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout

from backend.common.exceptions.app_error import AppError
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.asr.routing_service import AsrRoutingService
from backend.service.worker.worker_service import WorkerService


class RemotePoolService:
    """池持旧版全局锁，槽持稳定目标锁；不对模型部署或GPU容量作任何推断。"""

    def __init__(self, database, settings, logger, *, worker_factory=WorkerService,
                 remote_service=None, runtime=None, clock=monotonic):
        self.database, self.settings, self.logger = database, settings, logger
        self.worker_factory, self.clock = worker_factory, clock
        self.remote = remote_service or RemoteAsrService(database, settings, pooled=True)
        self.runtime = runtime or RuntimeRepository(database)
        self.session_id = uuid4().hex
        self.workers = []
        self._lock = FileLock(str(settings.database_path) + ".remote-asr-worker.lock", timeout=0)
        self._owns_lock = self._begun = self._draining = self._stopped = False
        self._next_report = 0.0
        self.last_error = None

    @property
    def draining(self):
        return self._draining

    @property
    def drained(self):
        return self._draining and all(not item._owns_lock or item.drained for item in self.workers)

    @property
    def can_exit(self):
        return False  # 错误也必须排空全部槽；不能沿单Worker路径提前退出池。

    def start(self):
        try:
            self._lock.acquire()
        except Timeout as exc:
            raise AppError("WORKER_ALREADY_RUNNING", "此数据库已有外部 ASR 调用池或旧版 Worker。", 409) from exc
        self._owns_lock = True
        self.remote.start_pool(self.session_id, os.getpid())
        self._begun = True
        if self.settings.remote_asr_routing_revision is not None:
            plans = AsrRoutingService(self.database, self.settings).worker_plans()
            if not 1 <= len(plans) <= 8:
                raise AppError("ASR_POOL_LIMIT_INVALID", "外部调用池总槽数必须在1至8之间。", 409)
        else:
            limit = self.remote.pool_limit()
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 8:
                raise AppError("ASR_POOL_LIMIT_INVALID", "外部调用池限额必须在1至8之间。", 409)
            plans = [dict(settings=self.settings, target_id="remote-default", slot_index=index)
                     for index in range(limit)]
        for plan in plans:
            worker = self.worker_factory(self.database, plan["settings"], self.logger,
                asr_profile="remote", remote_pooled=True, remote_target_id=plan["target_id"],
                remote_slot_index=plan["slot_index"])
            self.workers.append(worker)
            worker.start()
            self._report(force=True)
        self.logger.info("外部 ASR 调用池已启动 pool_session=%s slots=%s；等待各槽健康检查", self.session_id, len(plans))

    def request_drain(self):
        if not self._draining:
            # 同一事务先阻止全部槽的新受理/领取，再逐槽完成原有在途请求。
            self.remote.begin_drain()
            self._draining = True
        for worker in self.workers:
            if worker._owns_lock and not worker.draining:
                worker.request_drain()

    def tick(self):
        if not self._owns_lock or self._stopped:
            return
        if self._draining:
            self.request_drain()
        for worker in self.workers:
            if not worker._owns_lock:
                continue
            try:
                worker.tick()
            except Exception:
                self.logger.exception("调用槽控制异常 slot_session=%s，排空整个调用池", worker.session_id)
                self.last_error = "REMOTE_ASR_POOL_SLOT_ERROR"
                self.request_drain()
            if worker.draining or worker.state == "error":
                self.request_drain()
        self._report()

    def _report(self, *, force=False):
        now = self.clock()
        if not force and now < self._next_report:
            return True
        self._next_report = now + self.settings.runtime_heartbeat_seconds
        active = [worker for worker in self.workers if worker._owns_lock]
        ready = any(worker.model_ready and worker.state in {"ready", "running", "committing"} for worker in active)
        busy = any(worker.lease is not None for worker in active)
        state = ("stopped" if self._stopped else "stopping" if self._draining else
                 "running" if busy else "ready" if ready else "starting")
        try:
            self.runtime.report(component_id="remote_asr_worker", session_id=self.session_id,
                pid=os.getpid(), state=state, model_ready=ready and not self._stopped,
                active_task_id=None, child_pid=None, last_error=self.last_error)
            return True
        except Exception:
            self.logger.exception("调用池汇总报告暂时失败")
            return False

    def close(self):
        if not self._owns_lock:
            return True
        if self._begun:
            try:
                self.request_drain()
                if not self.drained:
                    self.tick()
                    return False
                confirmed = True
                for worker in self.workers:
                    # 不使用all(generator)短路；一个槽退出待确认不妨碍其他槽收尾。
                    if not worker.close():
                        confirmed = False
                if not confirmed:
                    self._report(force=True)
                    return False
                self._stopped = True
                if not self._report(force=True):
                    return False
            except Exception:
                self.logger.exception("调用池停止未确认，保留全局锁并继续排空")
                return False
        self._lock.release()
        self._owns_lock = False
        return True
