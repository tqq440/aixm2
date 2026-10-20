"""实验室传输的固定边界；不读取应用 .env 或自动连接。"""

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
REMOTE_HOST = "10.8.21.33"
REMOTE_PORT = 52222
REMOTE_USER = "swufe"
REMOTE_ROOT = "/home/swufe/FIC-Project/tyb"


@dataclass(frozen=True)
class LabSettings:
    project_root: Path
    ssh_dir: Path
    openssh_dir: Path

    @classmethod
    def local(cls):
        return cls(PROJECT_ROOT, Path.home() / ".ssh",
                   Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH")

    @property
    def identity(self):
        return self.ssh_dir / "id_ed25519_aixm_lab"

    @property
    def known_hosts(self):
        return self.ssh_dir / "known_hosts_aixm_lab"

    @property
    def bundle_root(self):
        return self.project_root / "runtime" / "deployment" / "bundles"
