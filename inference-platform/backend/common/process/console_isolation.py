"""受管模型脱离 Windows 控制台广播；保留 Pipe 和 Job Object 生命周期。"""

import ctypes
from ctypes import wintypes
import os


def detach_console() -> None:
    """仅在模型子进程中、加载原生运行库之前调用；无控制台时也可重复调用。"""
    if os.name != "nt":
        raise OSError("本版本模型控制台隔离仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole.argtypes = []
    kernel.FreeConsole.restype = wintypes.BOOL
    if not kernel.FreeConsole():
        raise ctypes.WinError(ctypes.get_last_error())
