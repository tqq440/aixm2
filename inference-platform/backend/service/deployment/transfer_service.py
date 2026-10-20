"""源码传输编排；不接触应用数据库、模型或运行中的服务。"""

from pathlib import Path

from backend.common.transfer.openssh_transport import OpenSSHTransport, TransferFailure
from backend.config.deployment.lab_settings import LabSettings, REMOTE_HOST, REMOTE_PORT, REMOTE_ROOT, REMOTE_USER
from backend.schema.deployment.transfer_schema import TransferRequest
from backend.service.deployment.source_bundle_service import build_bundle, source_path_allowed
from backend.utils.files.source_archive import (
    MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES, manifest_entries, plain_path, read_bytes, sha256, strict_json, verified_archive,
)


class TransferService:
    def __init__(self, settings: LabSettings, transport=None):
        self.settings = settings
        self.transport = transport or OpenSSHTransport(settings)

    def execute(self, request: TransferRequest):
        if request.action == "package":
            return build_bundle(self.settings.project_root, self.settings.bundle_root)
        prerequisites = self.transport.prerequisites()
        if not prerequisites["ready"]:
            raise TransferFailure("SSH_SETUP_REQUIRED", prerequisites["reason"] + " 缺少：" + ", ".join(prerequisites["missing"]))
        if request.action == "check":
            return {"destination": f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PORT}{REMOTE_ROOT}",
                    "authentication": "dedicated_key", "remote": self.transport.receiver("check")}
        if request.action == "verify":
            return self.transport.receiver("verify", request.bundle_id)
        # Upload always preflights before creating a local bundle or remote staging.
        preflight = self.transport.receiver("check")
        bundle = self._existing_bundle(request.bundle_id) if request.bundle_id else build_bundle(self.settings.project_root, self.settings.bundle_root)
        if isinstance(preflight.get("freeBytes"), int):
            required = (Path(bundle["archivePath"]).stat().st_size
                        + Path(bundle["manifestPath"]).stat().st_size
                        + bundle.get("totalBytes", 0) + 16 * 1024 * 1024)
            if preflight["freeBytes"] < required:
                raise TransferFailure("REMOTE_DISK_SPACE_LOW", "远端剩余空间不足以保存压缩包和解压源码；未开始上传。")
        self.transport.receiver("prepare", bundle["bundleId"])
        self.transport.upload(bundle)
        installed = self.transport.receiver("install", bundle["bundleId"])
        verified = self.transport.receiver("verify", bundle["bundleId"])
        for report in (installed, verified):
            if report.get("fileCount") != bundle["fileCount"] or report.get("totalBytes") != bundle["totalBytes"]:
                raise TransferFailure("RECEIVER_CONTENT_MISMATCH", "远端核验的文件数或大小与本地批次不同，不能判定成功。")
        return {"bundle": bundle, "installed": installed, "verified": verified, "servicesStarted": False}

    def _existing_bundle(self, bundle_id):
        folder = self.settings.bundle_root / bundle_id
        archive = folder / "source.zip"
        manifest = folder / "manifest.json"
        # Receiver is authoritative for the complete ZIP/manifest validation.
        # This check also catches a stale or changed local batch before SFTP.
        plain_path(folder)
        if not archive.is_file() or not manifest.is_file():
            raise ValueError("local bundle does not exist")
        if sha256(read_bytes(archive, MAX_ARCHIVE_BYTES)) != bundle_id:
            raise ValueError("local archive hash mismatch")
        entries = manifest_entries(strict_json(read_bytes(manifest, MAX_MANIFEST_BYTES)))
        if any(not source_path_allowed(entry["path"]) for entry in entries):
            raise ValueError("local bundle contains a non-source or secret path; upload refused")
        verified_archive(archive, entries)
        return {"bundleId": bundle_id, "archivePath": str(archive), "manifestPath": str(manifest),
                "fileCount": len(entries), "totalBytes": sum(entry["size"] for entry in entries)}
