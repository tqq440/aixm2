"""本地验收问题快照＋现有JSON运行日志；不连接网络或读写任务库。"""

from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
from itertools import islice
import re
from uuid import uuid4
from filelock import FileLock

from backend.common.logging.app_logging import configure_logging,close_logging
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.schema.deployment.validation_issue_schema import (
    ValidationDeferral,ValidationIssue,ValidationIssueQuery,ValidationIssueView,ValidationIssuePage,
)
from backend.utils.files.deployment_files import private_text
from backend.utils.files.source_archive import plain_path,read_bytes


class ValidationIssueLog:
    max_entries=1000
    def __init__(self,root=None,*,evidence_root=None):
        self.root=plain_path(Path(root) if root is not None else PROJECT_ROOT/"runtime/logs/validation",allow_missing=True)
        self.evidence_root=plain_path(Path(evidence_root) if evidence_root is not None else PROJECT_ROOT/"runtime/verification",allow_missing=True)

    def _snapshot_path(self,stage,target):
        identity=hashlib.sha256(json.dumps([stage,target],ensure_ascii=True).encode()).hexdigest()[:32]
        return self.root/("issue-"+identity+".json")

    def latest(self,stage,target):
        # 使用同一契约校验身份；不建目录、记录日志、探测或重新检查服务器。
        query=ValidationDeferral(stage=stage,target=target,code="LINUX_RUNTIME_UNVERIFIED")
        path=self._snapshot_path(query.stage,query.target)
        if not path.exists():return None
        snapshot=ValidationIssue.model_validate_json(read_bytes(path,65536))
        if snapshot.stage!=stage or snapshot.target!=target:raise ValueError("验收问题身份不匹配")
        return snapshot.model_dump(mode="json",by_alias=True)

    def listing(self,query):
        """只读且有界扫描；不返回证据路径、原响应或运行日志正文。"""
        query=ValidationIssueQuery.model_validate(query.model_dump())
        rows=[];unreadable=0;truncated=False
        if self.root.exists():
            with os.scandir(self.root) as entries:names=[entry.name for entry in islice(entries,self.max_entries+1)]
            truncated=len(names)>self.max_entries
            for name in names[:self.max_entries]:
                if not re.fullmatch(r"issue-[a-f0-9]{32}\.json",name):continue
                try:
                    item=ValidationIssue.model_validate_json(read_bytes(self.root/name,8192))
                    if self._snapshot_path(item.stage,item.target).name!=name:raise ValueError("Identity mismatch")
                    if item.first_observed_at.tzinfo is None or item.last_observed_at.tzinfo is None:raise ValueError("Timezone required")
                    item.last_observed_at.timestamp()
                    if query.stage is None or item.stage==query.stage:
                        rows.append(ValidationIssueView.model_validate(item.model_dump(exclude={"evidence_ref","schema_version"})))
                except (OSError,ValueError):unreadable+=1
        rows.sort(key=lambda item:(-item.last_observed_at.timestamp(),item.stage,item.target))
        return ValidationIssuePage(items=rows[query.offset:query.offset+query.limit],total=len(rows),offset=query.offset,
            limit=query.limit,unreadable_count=unreadable,scan_truncated=truncated,
            notice="仅显示本地已保存的验收记录；没有记录不代表实际环境已验收通过。刷新不会连接服务器或改变任务状态。")

    def defer(self,command):
        command=ValidationDeferral.model_validate(command.model_dump())
        evidence_hash=None
        if command.evidence_ref:
            evidence_hash=hashlib.sha256(read_bytes(self.evidence_root/command.evidence_ref,65536)).hexdigest()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700);plain_path(self.root)
        lock_path=plain_path(self.root/"issues.lock",allow_missing=True)
        path=self._snapshot_path(command.stage,command.target)
        with FileLock(str(lock_path),timeout=3):
            previous=self.latest(command.stage,command.target)
            now=datetime.now(timezone.utc)
            snapshot=ValidationIssue(**command.model_dump(),first_observed_at=previous["firstObservedAt"] if previous else now,
                last_observed_at=now,observation_count=previous["observationCount"]+1 if previous else 1,evidence_sha256=evidence_hash)
            temporary=self.root/(".issue-"+uuid4().hex+".tmp")
            try:
                private_text(temporary,snapshot.model_dump_json(by_alias=True,indent=2)+'\n')
                plain_path(path,allow_missing=True);os.replace(temporary,path)
            finally:
                if temporary.exists():temporary.unlink()
        # 运行日志失败不撤销已经持久化的问题快照，也不触碰任务结果。
        logger=None;written=False
        try:
            logger=configure_logging(self.root,"INFO","environment-validation.log",process_scoped=True)
            errors=[]
            for handler in logger.handlers:handler.handleError=lambda _record:errors.append(True)
            logger.warning("VALIDATION_DEFERRED stage=%s target=%s code=%s source=%s evidenceSha256=%s",
                command.stage,command.target,command.code,command.source,evidence_hash or "none")
            for handler in logger.handlers:handler.flush()
            written=not errors
        except OSError:pass
        finally:
            if logger:close_logging(logger)
        return {"issue":snapshot.model_dump(mode="json",by_alias=True),"snapshotSaved":True,
            "runtimeLogWritten":written,"snapshotPath":str(path),"taskStateChanged":False}
