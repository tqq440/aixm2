"""本地维护命令契约；不提供任意服务器或远端命令参数。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.config.deployment.lab_settings import REMOTE_ROOT


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["check", "package", "upload", "verify"]
    bundle_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_bundle(self):
        if self.action == "verify" and self.bundle_id is None:
            raise ValueError("verify requires the exact bundle ID")
        if self.action in {"check", "package"} and self.bundle_id is not None:
            raise ValueError("this action does not accept a bundle ID")
        return self


class RemoteCheck(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    root: str = Field(max_length=512)
    rootExists: bool
    targetPath: str = Field(max_length=512)
    targetState: Literal["absent", "empty", "present"]
    freeBytes: int = Field(ge=0)
    pythonSupported: Literal[True]
    pythonVersion: str = Field(min_length=1, max_length=64)
    operatingSystem: Literal["Linux"]
    rootWritable: bool

    @model_validator(mode="after")
    def fixed_destination(self):
        if self.root != REMOTE_ROOT or self.targetPath != REMOTE_ROOT + "/inference-platform":
            raise ValueError("unexpected receiver destination")
        return self


class RemotePrepared(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundleId: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["prepared"]
    incomingPath: str
    archivePath: str
    manifestPath: str

    @model_validator(mode="after")
    def fixed_staging(self):
        base = REMOTE_ROOT + "/.aixm-incoming/" + self.bundleId
        if (self.incomingPath, self.archivePath, self.manifestPath) != (base, base + "/source.zip", base + "/manifest.json"):
            raise ValueError("unexpected receiver staging")
        return self


class RemoteInstalled(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundleId: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["installed", "already_installed", "verified"]
    fileCount: int = Field(ge=1, le=10000)
    totalBytes: int = Field(ge=0, le=256 * 1024 * 1024)
    servicesStarted: Literal[False]
    dependenciesInstalled: Literal[False]


def validate_receiver_result(action: str, bundle_id: str | None, payload: dict) -> dict:
    if action == "check":
        RemoteCheck.model_validate(payload)
    elif action == "prepare":
        RemotePrepared.model_validate(payload)
    else:
        report = RemoteInstalled.model_validate(payload)
        expected = {"verified"} if action == "verify" else {"installed", "already_installed"}
        if report.status not in expected:
            raise ValueError("receiver reported a different operation")
    if action != "check" and payload["bundleId"] != bundle_id:
        raise ValueError("receiver reported a different bundle")
    return payload
