"""固定受信命令的有界只读输出；不执行 shell，不将参数写入异常。"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
import subprocess
from time import monotonic


class CommandFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def run_bounded_command(command, *, timeout=3, max_bytes=65536):
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = monotonic() + timeout
        with ThreadPoolExecutor(max_workers=1) as reader:
            future = reader.submit(process.stdout.read, max_bytes + 1)
            try:
                output = future.result(timeout=timeout)
                if len(output) > max_bytes:
                    raise CommandFailure("OUTPUT_TOO_LARGE")
                code = process.wait(timeout=max(0.01, deadline - monotonic()))
            except (FutureTimeout, subprocess.TimeoutExpired):
                process.kill(); process.wait(timeout=2)
                raise CommandFailure("COMMAND_TIMEOUT") from None
            except Exception:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=2)
                raise
        if code != 0:
            raise CommandFailure("COMMAND_FAILED")
        return output
    except OSError:
        raise CommandFailure("COMMAND_UNAVAILABLE") from None
    finally:
        if process is not None and process.stdout:
            process.stdout.close()
