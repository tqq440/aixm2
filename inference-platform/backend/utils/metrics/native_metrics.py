"""标准库只读主机采样；不设置资源限制或修改设备。"""

import csv
import ctypes
import io
import math
from pathlib import Path
import sys


def read_cpu_memory(*, platform=None, proc_root=Path("/proc")):
    platform = platform or sys.platform
    if platform == "linux":
        with (proc_root / "stat").open(encoding="ascii") as stream:
            values = [int(value) for value in stream.readline().split()[1:9]]
        if len(values) != 8 or any(v < 0 for v in values):
            raise ValueError("CPU sample unavailable")
        memory = {}
        with (proc_root / "meminfo").open(encoding="ascii") as stream:
            for line in stream:
                key, rest = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    number, unit = rest.split()
                    if unit != "kB":
                        raise ValueError("Memory unit unavailable")
                    memory[key] = int(number) * 1024
        total, available = memory["MemTotal"], memory["MemAvailable"]
        sample = (sum(values), values[3] + values[4])
    elif platform == "win32":
        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]
            def value(self):
                return self.low + (self.high << 32)
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                ("total", ctypes.c_uint64), ("available", ctypes.c_uint64),
                ("pageTotal", ctypes.c_uint64), ("pageAvailable", ctypes.c_uint64),
                ("virtualTotal", ctypes.c_uint64), ("virtualAvailable", ctypes.c_uint64), ("extended", ctypes.c_uint64)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        idle, system, user = FileTime(), FileTime(), FileTime()
        if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(system), ctypes.byref(user)):
            raise OSError("CPU sample unavailable")
        status = MemoryStatus(); status.length = ctypes.sizeof(status)
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("Memory sample unavailable")
        total, available = status.total, status.available
        sample = (system.value() + user.value(), idle.value())
    else:
        raise OSError("Platform metrics unavailable")
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("Memory sample unavailable")
    return sample, {"totalBytes": total, "availableBytes": available, "usedBytes": total - available,
                    "usedPercent": round((total - available) * 100 / total, 2)}


def cpu_percent(previous, current):
    if previous is None:
        return None
    total, idle = current[0] - previous[0], current[1] - previous[1]
    if total <= 0 or idle < 0 or idle > total:
        return None
    return round((total - idle) * 100 / total, 2)


def parse_gpu_csv(content):
    rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))
    if len(rows) > 64:
        raise ValueError("Too many GPUs")
    result = []
    for row in rows:
        if len(row) != 6:
            raise ValueError("Invalid GPU columns")
        index, uuid, name, total, used, utilization = [value.strip() for value in row]
        def number(value, maximum=None):
            if value in {"N/A", "[N/A]", "Not Supported", "[Not Supported]"}:
                return None
            parsed = float(value)
            if not math.isfinite(parsed) or parsed < 0 or (maximum is not None and parsed > maximum):
                raise ValueError("Invalid GPU metric")
            return parsed
        if not index.isdigit() or len(uuid) > 96 or not uuid.startswith("GPU-") or len(name) > 256:
            raise ValueError("Invalid GPU identity")
        total, used, utilization = number(total), number(used), number(utilization, 100)
        if total is not None and used is not None and used > total:
            raise ValueError("Invalid GPU memory")
        result.append({"index": int(index), "uuid": uuid, "name": name,
            "memoryTotalBytes": int(total * 1024 * 1024) if total is not None else None,
            "memoryUsedBytes": int(used * 1024 * 1024) if used is not None else None, "utilizationPercent": utilization})
    if len({gpu["uuid"] for gpu in result}) != len(result):
        raise ValueError("Duplicate GPU identity")
    return result
