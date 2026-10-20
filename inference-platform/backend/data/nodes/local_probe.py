"""只读采集真实 Windows 本机身份；不回退随机值，不探测 GPU。"""

import os
import platform

from backend.utils.identity.identity_utils import hash_namespaced_uuid


def probe_local_node() -> dict:
    if platform.system() != "Windows":
        raise OSError("本阶段只支持 Windows 本机节点登记。")
    try:
        # 仅在 Windows 上加载，其他系统不会被错误当作 Windows 身份来源。
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            machine_guid, value_type = winreg.QueryValueEx(key, "MachineGuid")
        if value_type != winreg.REG_SZ:
            raise ValueError("本机身份注册表值类型无效。")
        local_identity = hash_namespaced_uuid(machine_guid, namespace="windows")
        values = {
            "hostname": platform.node(),
            "os_name": platform.system(),
            "os_version": platform.version(),
            "architecture": platform.machine(),
        }
        bounds = {"hostname": 255, "os_name": 64, "os_version": 255, "architecture": 64}
        if any(not isinstance(value, str) or not value.strip() or len(value) > bounds[name]
               for name, value in values.items()):
            raise ValueError("本机系统信息为空或不符合长度限制。")
        cpu_count = os.cpu_count()
        if cpu_count is not None and (type(cpu_count) is not int or cpu_count < 1):
            raise ValueError("本机逻辑处理器数无效。")
        return {"local_identity": local_identity, **values, "cpu_count": cpu_count}
    except (OSError, ValueError, TypeError, ImportError):
        # 底层注册表/UUID 异常可能携带原始 MachineGuid，不向上层传播其正文或链。
        raise OSError("无法读取有效的 Windows 本机身份或系统信息。") from None
