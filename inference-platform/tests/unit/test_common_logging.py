"""运行日志写入失败不传播；公共模式按进程分别滚动文件。"""

import json
import subprocess
import sys

from backend.common.logging.app_logging import close_logging, configure_logging
from backend.config.settings.app_settings import PROJECT_ROOT, load_settings


def test_common_flag_defaults_off_and_can_be_explicitly_loaded(tmp_path):
    assert not load_settings(env_file=tmp_path / "absent", environ={}).common_execution_enabled
    assert load_settings(env_file=tmp_path / "absent", environ={"INFERENCE_COMMON_EXECUTION_ENABLED": "true"}).common_execution_enabled


def test_two_real_processes_write_separate_json_logs(tmp_path):
    code = "from pathlib import Path; import sys; from backend.common.logging.app_logging import configure_logging, close_logging; logger=configure_logging(Path(sys.argv[1]),'INFO',process_scoped=True); logger.info('isolated process'); close_logging(logger)"
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], cwd=PROJECT_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        _, error = child.communicate(timeout=20)
        assert child.returncode == 0, error
    files = list(tmp_path.glob("api-*.log"))
    assert len(files) == 2
    assert {path.name for path in files} == {f"api-{child.pid}.log" for child in children}
    for path in files:
        assert json.loads(path.read_text(encoding="utf-8").strip())["message"] == "isolated process"


def test_file_write_failure_does_not_propagate_to_caller(tmp_path):
    writes = []
    class Unwritable:
        def write(self, _):
            writes.append(True)
            raise OSError("isolated log disk failure")
        def flush(self): pass
        def __getattr__(self, name): return getattr(original, name)
    logger = configure_logging(tmp_path, "INFO", process_scoped=True)
    file_handler = next(handler for handler in logger.handlers if hasattr(handler, "baseFilename"))
    original = file_handler.stream
    file_handler.stream = Unwritable()
    try:
        logger.info("committed task outcome")
        assert writes == [True]
    finally:
        file_handler.stream = original
        close_logging(logger)
