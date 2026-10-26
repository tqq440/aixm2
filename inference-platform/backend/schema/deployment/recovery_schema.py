"""仅支持一套显式6A独立验证环境，恢复始终隔离。"""

from datetime import datetime
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator, model_validator
from backend.schema.common.api_schema import ApiSchema
from backend.schema.catalog.service_schema import Identifier, Revision
from backend.utils.files.source_archive import archive_path, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES
from backend.utils.time.aware_time import require_aware_datetime


class SnapshotRequest(ApiSchema):
    root: Path
    output: Path
    controllers_stopped_confirmed: Literal[True]

    @field_validator("controllers_stopped_confirmed",mode="before")
    @classmethod
    def explicit_confirmation(cls,value):
        if value is not True:raise ValueError("需要明确确认中心、执行器和Node控制进程已停止")
        return value

    @field_validator("root","output")
    @classmethod
    def absolute_path(cls,value):
        if not value.is_absolute():raise ValueError("必须显式提供绝对路径")
        return value


class RestoreRequest(ApiSchema):
    snapshot: Path
    output: Path

    @field_validator("snapshot","output")
    @classmethod
    def absolute_path(cls,value):
        if not value.is_absolute():raise ValueError("必须显式提供绝对路径")
        return value


class SnapshotFile(ApiSchema):
    path: str
    size: int=Field(ge=0,le=MAX_FILE_BYTES,strict=True)
    sha256: Revision

    @field_validator("path")
    @classmethod
    def allowed_path(cls,value):
        value=archive_path(value)
        exact={"db/center.sqlite3","db/node.sqlite3","db/center.sqlite3.asr-services.json",
               "db/center.sqlite3.restore-quarantine.json","db/node.sqlite3.restore-quarantine.json",
               "config/center.private.json","config/node.private.json"}
        if value not in exact and not value.startswith(("uploads/","logs/center/","logs/node/")):
            raise ValueError("快照包含范围外文件")
        return value


class SnapshotManifest(ApiSchema):
    schema_version: Literal[1]=1
    snapshot_id: str=Field(pattern=r"^[a-f0-9]{32}$")
    created_at: datetime
    purpose: Literal["isolated-validation-recovery"]="isolated-validation-recovery"
    source_name: str=Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    node_id: Identifier
    center_head: Literal["20261022_0021"]="20261022_0021"
    node_head: Literal["20261021_node_0004"]="20261021_node_0004"
    files: list[SnapshotFile]=Field(min_length=6,max_length=MAX_FILES-2)

    @field_validator("schema_version",mode="before")
    @classmethod
    def strict_version(cls,value):
        if type(value) is not int or value!=1:raise ValueError("不支持的快照格式")
        return value

    @field_validator("created_at",mode="before")
    @classmethod
    def aware_time(cls,value):return require_aware_datetime(value)

    @model_validator(mode="after")
    def complete_inventory(self):
        names=[entry.path.casefold() for entry in self.files]
        required={"db/center.sqlite3","db/node.sqlite3","config/center.private.json","config/node.private.json",
                  "db/center.sqlite3.restore-quarantine.json","db/node.sqlite3.restore-quarantine.json"}
        if len(set(names))!=len(names) or not required.issubset(names):
            raise ValueError("快照清单重复或缺少成套资料")
        if sum(entry.size for entry in self.files)>MAX_TOTAL_BYTES:
            raise ValueError("快照超过总大小上限")
        return self


class RecoveryFence(ApiSchema):
    snapshot_id: str=Field(pattern=r"^[a-f0-9]{32}$")
    state: Literal["quarantined"]="quarantined"
    automatic_execution: Literal[False]=False
