"""本机服务管理命令与持久状态的边界校验。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlatformOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_path: Path = Path("D:/develop/node/node.exe")
    frontend_port: int = Field(default=5173, ge=1024, le=65535)
    startup_timeout_seconds: float = Field(default=180, ge=5, le=900, allow_inf_nan=False)

    @field_validator("node_path", mode="before")
    @classmethod
    def executable_path(cls, value):
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Node 可执行文件不存在")
        return path


class PlatformState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["starting", "running", "stopping", "stopped", "failed"]
    manager_pid: int
    api_port: int
    frontend_port: int
    updated_at: float
    message: str
    log_dir: str
    processes: dict[str, int] = Field(default_factory=dict)
    exit_codes: dict[str, int | None] = Field(default_factory=dict)


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
