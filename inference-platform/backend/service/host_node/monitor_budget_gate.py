"""Node 持久记录许可申请/归还，不因重启遗失中心查询占位。"""

from datetime import datetime, timedelta
from backend.utils.time.time_utils import as_utc
from uuid import uuid4
from sqlalchemy import select
from backend.common.exceptions.app_error import AppError
from backend.data.host_node.execution_models import HostNodeExecution
from backend.schema.monitoring.budget_schema import MonitorPermitCommand
from backend.service.host_node.execution_service import TERMINAL
from backend.utils.time.iso_time import iso_utc


class MonitorBudgetGate:
    def __init__(self, executions, client):
        self.executions, self.client = executions, client

    def _save(self, session, row, **fields):
        previous = row.monitoring_json or {}
        row.monitoring_json = {**previous, **fields}
        reason = row.monitoring_json.get("queryBudgetReason")
        if reason and previous.get("lastBudgetEventReason") != reason:
            row.monitoring_json = {**row.monitoring_json, "lastBudgetEventReason": reason}
            row.updated_at = self.executions.node._now(session)
            self.executions._event(session, row, "MONITOR_BUDGET_CHANGED", details={
                "budgetState": row.monitoring_json.get("queryBudgetState"),
                "reason": row.monitoring_json.get("queryBudgetReason")})

    def recover(self):
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            for row in session.scalars(select(HostNodeExecution)):
                if (row.monitoring_json or {}).get("budgetPhase") == "querying":
                    self._save(session, row, budgetPhase="uncertain", queryBudgetState="uncertain",
                               queryBudgetReason="MONITOR_QUERY_UNCONFIRMED")

    def outstanding(self):
        with self.executions._transaction() as session:
            now=self.executions.node._now(session)
            return [row.invocation_id for row in session.scalars(select(HostNodeExecution))
                    if (row.monitoring_json or {}).get("budgetPhase") in {"pending", "release_pending"}
                    and (not (row.monitoring_json or {}).get("budgetRetryAt") or
                         now>=as_utc(datetime.fromisoformat(row.monitoring_json["budgetRetryAt"])).replace(tzinfo=None))]

    def release(self, identifier, *, confirmed=False):
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row = session.get(HostNodeExecution, identifier)
            m = row.monitoring_json or {}
            if not m.get("budgetPollId"):
                return True
            if m.get("budgetPhase") in {"querying", "uncertain"} and not confirmed:
                return False
            request = MonitorPermitCommand(poll_id=m["budgetPollId"], command=row.command_json)
            self._save(session, row, budgetPhase="release_pending")
        # 与 acquire 一样，归还也不能在 SQLite 写锁内通信。
        try:
            reply = self.client.change(request, release=True)
            if reply.state != "released":
                raise ValueError()
        except Exception:
            with self.executions._transaction(immediate=True) as session:
                self.executions.node._require_owner(session)
                row=session.get(HostNodeExecution,identifier)
                failures=min((row.monitoring_json or {}).get("budgetFailures",0)+1,10)
                retry=iso_utc(self.executions.node._now(session)+timedelta(seconds=min(5*2**(failures-1),60)))
                self._save(session,row,queryBudgetState="release_pending",queryBudgetReason="MONITOR_RELEASE_PENDING",
                           budgetFailures=failures,budgetRetryAt=retry,nextPollAt=retry)
            return False
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row = session.get(HostNodeExecution, identifier)
            if (row.monitoring_json or {}).get("budgetPollId") == request.poll_id:
                self._save(session, row, budgetPollId=None, budgetPhase=None, budgetRetryAt=None,
                           queryBudgetState=(row.monitoring_json or {}).get("queryBudgetState") if (row.monitoring_json or {}).get("queryBudgetState")=="waiting" else None,
                           queryBudgetReason=(row.monitoring_json or {}).get("queryBudgetReason") if (row.monitoring_json or {}).get("queryBudgetState")=="waiting" else None)
        return True

    def acquire(self, identifier):
        if not self.release(identifier):
            return None
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row = session.get(HostNodeExecution, identifier)
            if row.state in TERMINAL:
                return None
            request = MonitorPermitCommand(poll_id=uuid4().hex, command=row.command_json)
            # 在发送申请前留恢复记录；响应丢失后先归还/墓碑，再申请新编号。
            self._save(session, row, budgetPollId=request.poll_id, budgetPhase="pending")
        try:
            reply = self.client.change(request)
            if reply.poll_id != request.poll_id:
                raise ValueError()
        except Exception:
            self.wait(identifier, "MONITOR_BUDGET_UNAVAILABLE")
            return None
        if reply.state != "held":
            self.wait(identifier, reply.reason or "MONITOR_BUDGET_UNAVAILABLE")
            return None
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row = session.get(HostNodeExecution, identifier)
            if row.state not in TERMINAL and (row.monitoring_json or {}).get("budgetPollId") == request.poll_id:
                self._save(session, row, budgetPhase="querying", queryBudgetState="querying", queryBudgetReason=None, lastBudgetEventReason=None, budgetFailures=0, budgetRetryAt=None)
                return request
        self.release(identifier, confirmed=True)
        return None

    def wait(self, identifier, reason):
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row = session.get(HostNodeExecution, identifier)
            now = self.executions.node._now(session)
            failures=min((row.monitoring_json or {}).get("budgetFailures",0)+1,10)
            delay=min(5*2**(failures-1),60) if reason=="MONITOR_BUDGET_UNAVAILABLE" else 5
            retry=iso_utc(now+timedelta(seconds=delay))
            self._save(session, row, queryBudgetState="waiting", queryBudgetReason=reason,
                       budgetFailures=failures,budgetRetryAt=retry,nextPollAt=retry)

    def confirm_stopped(self, identifier):
        """仅管理员明确核对原查询已停止后调用；不改变推理状态或容量。"""
        if not self.release(identifier, confirmed=True):
            raise AppError("MONITOR_RELEASE_PENDING", "中心暂不可确认归还，保留查询占位。", 503)
        with self.executions._transaction(immediate=True) as session:
            self.executions.node._require_owner(session)
            row=session.get(HostNodeExecution,identifier)
            self._save(session,row,queryBudgetState=None,queryBudgetReason=None,lastBudgetEventReason=None)
        return self.executions.get(identifier)
