"""仅合成资料的旧库迁移演练契约；不接受现有数据库路径。"""

from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator
from backend.schema.common.api_schema import ApiSchema
from backend.utils.files.source_archive import archive_path, MAX_FILE_BYTES


class MigrationRehearsalRequest(ApiSchema):
    output: Path

    @field_validator("output")
    @classmethod
    def absolute_output(cls, value):
        if not value.is_absolute():
            raise ValueError("演练输出必须是全新绝对路径")
        return value


class RehearsalFile(ApiSchema):
    path: str
    size: int = Field(ge=0, le=MAX_FILE_BYTES, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def fixture_file(cls, value):
        value = archive_path(value)
        if value == "RESTORE_QUARANTINED.json":
            return value
        parts = value.split("/")
        if len(parts) != 3 or parts[0] not in ("legacy", "upgraded"):
            raise ValueError("演练文件越界")
        if parts[1:] in (["db", "center.sqlite3"], ["db", "center.sqlite3.restore-quarantine.json"],
                         ["db", "center.sqlite3.asr-services.json"]):
            return value
        if parts[1] == "uploads" and len(parts[2]) == 38 and parts[2][-6:] in (".audio", ".image"):
            if all(c in "0123456789abcdef" for c in parts[2][:32]):
                return value
        raise ValueError("不接受任意历史文件")


class TableEvidence(ApiSchema):
    rows: int = Field(ge=0, le=10000, strict=True)
    columns: list[str] = Field(min_length=1, max_length=100)
    rows_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MigrationEvidence(ApiSchema):
    schema_version: Literal[1] = 1
    purpose: Literal["synthetic-0014-migration-rehearsal"] = "synthetic-0014-migration-rehearsal"
    rehearsal_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_revision: Literal["20261020_0014"] = "20261020_0014"
    target_revision: Literal["20261022_0021"] = "20261022_0021"
    tables: dict[str, TableEvidence] = Field(min_length=26, max_length=26)
    files: list[RehearsalFile] = Field(min_length=23, max_length=23)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("演练证据版本必须是整数")
        return value
