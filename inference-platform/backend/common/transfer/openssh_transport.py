"""非交互 OpenSSH 传输；凭据解锁由用户终端负责。"""

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from backend.config.deployment.lab_settings import REMOTE_HOST, REMOTE_PORT, REMOTE_ROOT, REMOTE_USER, LabSettings
from backend.schema.deployment.transfer_schema import validate_receiver_result


class TransferFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OpenSSHTransport:
    def __init__(self, settings: LabSettings):
        self.settings = settings

    def prerequisites(self):
        paths = {
            "ssh": self.settings.openssh_dir / "ssh.exe",
            "sftp": self.settings.openssh_dir / "sftp.exe",
            "sshAdd": self.settings.openssh_dir / "ssh-add.exe",
            "privateKey": self.settings.identity,
            "publicKey": Path(str(self.settings.identity) + ".pub"),
            "verifiedHostKey": self.settings.known_hosts,
        }
        try:
            missing = [name for name, path in paths.items() if not path.is_file()]
        except OSError:
            return {"ready": False, "missing": ["credentialPathAccess"],
                    "reason": "当前执行环境无权读取专用SSH文件状态，请使用正常Windows用户权限核查。"}
        if missing:
            return {"ready": False, "missing": missing, "reason": "请先在自己的终端完成 setup-lab-ssh.ps1 登录准备。"}
        # ssh-add -L only exposes public keys; no private material is read or logged.
        try:
            result = subprocess.run([str(paths["sshAdd"]), "-L"], capture_output=True,
                                    timeout=8, check=False)
            public_key = paths["publicKey"].read_text(encoding="utf-8-sig").split()
            agent_keys = [line.split()[:2] for line in result.stdout.decode("utf-8", "replace").splitlines()]
            unlocked = len(public_key) >= 2 and public_key[0] == "ssh-ed25519" and public_key[:2] in agent_keys
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            unlocked = False
        return {"ready": unlocked, "missing": [] if unlocked else ["unlockedDedicatedKey"],
                "reason": "密钥已由本机代理解锁。" if unlocked else "请运行 setup-lab-ssh.ps1 -Action Unlock。"}

    def _options(self):
        # Ignore unrelated per-user config, proxy commands, forwarding and identities.
        null_file = "NUL" if os.name == "nt" else "/dev/null"
        return ["-F", null_file, "-i", str(self.settings.identity) + ".pub",
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-o", "StrictHostKeyChecking=yes",
                "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UpdateHostKeys=no",
                "-o", f"UserKnownHostsFile={self.settings.known_hosts}",
                "-o", f"GlobalKnownHostsFile={null_file}", "-o", "ForwardAgent=no",
                "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
                "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]

    def _run(self, command, *, data: bytes, cwd: Path | None = None, timeout: int = 60):
        try:
            result = subprocess.run(command, input=data, capture_output=True, cwd=cwd,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TransferFailure("TRANSFER_TIMEOUT", "传输或核验超时；不要推断远端已安装，先按批次核验。") from exc
        except OSError as exc:
            raise TransferFailure("OPENSSH_UNAVAILABLE", "无法运行本机 OpenSSH，请检查安装与执行权限。") from exc
        if result.returncode:
            error = result.stderr.decode("utf-8", "replace").lower()
            if "host key" in error or "host identification has changed" in error:
                code, message = "HOST_KEY_REJECTED", "服务器主机密钥未通过固定记录校验，未绕过检查。"
            elif "permission denied" in error:
                code, message = "SSH_AUTH_OR_PERMISSION_DENIED", "SSH认证或远端目录权限不足，请核对公钥登记和目录权限。"
            elif "connection timed out" in error or "connection refused" in error:
                code, message = "SSH_UNREACHABLE", "固定SSH地址不可达，请核对校园网与服务器连接。"
            else:
                code, message = "REMOTE_OPERATION_FAILED", "远端命令或传输失败；原始输出未回显，避免泄露连接环境。"
            # The controlled receiver writes a bounded JSON error to stdout.
            try:
                payload = json.loads(result.stdout) if len(result.stdout) <= 1024 * 1024 else {}
                if isinstance(payload, dict) and payload.get("ok") is False:
                    message = "远端校验拒绝本批次：" + str(payload.get("error", "检查失败"))[:300]
            except (ValueError, UnicodeError):
                pass
            raise TransferFailure(code, message)
        return result.stdout

    def _receiver_program(self, action: str, bundle_id: str | None):
        if action not in {"check", "prepare", "install", "verify"}:
            raise ValueError("unsupported remote operation")
        if action != "check" and not re.fullmatch(r"[0-9a-f]{64}", bundle_id or ""):
            raise ValueError("invalid bundle ID")
        # Load only these checked-in, fixed infrastructure modules into memory.
        # This bootstrap does not require platform dependencies on the new server.
        modules = ["backend.utils.files.source_archive", "backend.service.deployment.source_bundle_service",
                   "backend.service.deployment.source_receiver"]
        sources = []
        for name in modules:
            source = self.settings.project_root.joinpath(*name.split(".")).with_suffix(".py").read_bytes()
            sources.append((name, base64.b64encode(source).decode("ascii")))
        return ("import sys,types,base64,json\n"
                "if sys.version_info < (3,10): raise SystemExit('Python 3.10+ required for transfer bootstrap')\n"
                "for name in ('backend','backend.utils','backend.utils.files','backend.service','backend.service.deployment'):\n"
                " m=types.ModuleType(name); m.__path__=[]; sys.modules[name]=m\n"
                f"for name, encoded in {sources!r}:\n"
                " m=types.ModuleType(name); m.__file__='<lab-transfer>/'+name; sys.modules[name]=m\n"
                " exec(compile(base64.b64decode(encoded),m.__file__,'exec'),m.__dict__)\n"
                "from pathlib import Path\n"
                "from backend.service.deployment.source_receiver import Receiver\n"
                "try:\n"
                f" receiver=Receiver(Path({REMOTE_ROOT!r}))\n"
                f" result=receiver.{action}({'' if action == 'check' else repr(bundle_id)})\n"
                " print(json.dumps({'ok':True,'result':result},ensure_ascii=True))\n"
                "except (OSError,ValueError,RuntimeError) as exc:\n"
                " print(json.dumps({'ok':False,'error':str(exc)[:300]},ensure_ascii=True));sys.exit(2)\n").encode("utf-8")

    def receiver(self, action: str, bundle_id: str | None = None):
        command = [str(self.settings.openssh_dir / "ssh.exe"), *self._options(), "-T",
                   "-p", str(REMOTE_PORT), f"{REMOTE_USER}@{REMOTE_HOST}", "python3 -"]
        output = self._run(command, data=self._receiver_program(action, bundle_id), timeout=120)
        try:
            if len(output) > 1024 * 1024:
                raise ValueError("oversized output")
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("result"), dict):
                raise ValueError("invalid receiver response")
            return validate_receiver_result(action, bundle_id, result["result"])
        except (ValueError, UnicodeError) as exc:
            raise TransferFailure("INVALID_RECEIVER_RESPONSE", "远端没有返回有效核验结果，不能判定成功。") from exc

    def upload(self, bundle: dict):
        bundle_id = bundle["bundleId"]
        if not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
            raise ValueError("invalid bundle ID")
        folder = Path(bundle["archivePath"]).parent
        if Path(bundle["archivePath"]).name != "source.zip" or Path(bundle["manifestPath"]) != folder / "manifest.json":
            raise ValueError("unexpected local bundle layout")
        destination = f"{REMOTE_ROOT}/.aixm-incoming/{bundle_id}"
        # Restart an interrupted staging upload; never overwrite an active source tree.
        batch = (f'put "source.zip" "{destination}/source.zip"\n'
                 f'put "manifest.json" "{destination}/manifest.json"\n').encode("ascii")
        command = [str(self.settings.openssh_dir / "sftp.exe"), *self._options(),
                   "-P", str(REMOTE_PORT), "-b", "-", f"{REMOTE_USER}@{REMOTE_HOST}"]
        self._run(command, data=batch, cwd=folder, timeout=300)
