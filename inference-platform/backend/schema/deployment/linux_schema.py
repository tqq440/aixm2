"""离线 Linux 部署资料；路径不插入任意 shell 内容。"""

from pathlib import PurePosixPath,Path
import re
from pydantic import Field,field_validator,model_validator
from backend.schema.common.api_schema import ApiSchema


class LinuxDeploymentPlan(ApiSchema):
    name: str = Field(default="module6a",pattern=r"^[a-z][a-z0-9-]{0,31}$")
    project_dir: str
    python_executable: str
    state_root: str
    center_port: int = Field(default=15000,ge=1024,le=65535)
    node_port: int = Field(default=18020,ge=1024,le=65535)
    ui_port: int = Field(default=15173,ge=1024,le=65535)
    public_proxy_port: int = Field(default=18080,ge=1024,le=65535)
    threads: int = Field(default=16,ge=4,le=64)

    @field_validator("project_dir","python_executable","state_root")
    @classmethod
    def canonical_path(cls,value):
        if value=="/" or not re.fullmatch(r"/[A-Za-z0-9._/-]+",value) or str(PurePosixPath(value))!=value or any(p in {".",".."} for p in value.split('/')[1:]):
            raise ValueError("使用不含空白、变量或穿越片段的规范Linux绝对路径")
        return value

    @model_validator(mode="after")
    def isolated(self):
        expected=PurePosixPath(self.project_dir)/"runtime/deployment-validation"/self.name
        if PurePosixPath(self.state_root)!=expected:raise ValueError("状态目录必须为项目runtime/deployment-validation/部署名")
        ports=(self.center_port,self.node_port,self.ui_port,self.public_proxy_port)
        if len(set(ports))!=4:raise ValueError("四个监听端口不能重复")
        return self


class ValidationWorkspace(ApiSchema):
    root: Path
    name: str = Field(default="module6a",pattern=r"^[a-z][a-z0-9-]{0,31}$")
    center_port: int = Field(default=15000,ge=1024,le=65535)
    node_port: int = Field(default=18020,ge=1024,le=65535)

    @model_validator(mode="after")
    def distinct_ports(self):
        if self.center_port==self.node_port:raise ValueError("中心和Node端口不能相同")
        if not self.root.is_absolute():raise ValueError("验证目录须为显式绝对路径")
        return self
